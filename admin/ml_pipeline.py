"""
Базовый ML-пайплайн CAM: сборка выборки из БД → выбор модели → скоринг.

Ориентир — cam_model_v7 / cam_inference (LogReg, 15 фич, AUC 0.868),
но с тремя исправлениями против утечек:

  1. GroupKFold по застройщику (ИНН): все объекты одного застройщика
     целиком в train ИЛИ в test — модель не «узнаёт знакомых»;
  2. агрегаты застройщика/подрядчика (built/bad) считаются ТОЛЬКО по
     train-фолду и переносятся на test по ИНН — незнакомый застройщик
     получает NaN → импутация, как будет и в проде с новыми компаниями;
  3. никаких пост-фактум признаков (days_overdue и т.п. в фичах нет).

Команды:
    python3 ml_pipeline.py train    # сборка, сравнение моделей, сохранение
    python3 ml_pipeline.py score    # скоринг активных объектов в БД
    python3 ml_pipeline.py dataset  # только показать выборку (отладка)
"""
import json
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
MODELS_DIR = BASE / 'models'
MODEL_PATH = MODELS_DIR / 'cam_model_v8.pkl'
TEST_IDS_PATH = MODELS_DIR / 'test_ids_v8.json'

# карты из v7
RATING_MAP = {'AAA': 9, 'AA': 8, 'A': 7, 'BBB': 6, 'BB': 5, 'B': 4,
              'CCC': 3, 'CC': 2, 'C': 1, 'DDD': -1, 'DD': -2, 'D': -3}
DIFF_MAP = {'I': 1, 'II': 2, 'III': 3, 'IV': 4}

# возраст компаний исключён: на текущей выборке стабильно ухудшает AUC
# (тест абляции: -0.03), вернуть при росте выборки/покрытия
# blocks_total исключён: абляция на valid (GroupKFold-DEV) показала AUC
# 0.666->0.696 и std 0.065->0.021 без него — шумная/переобучающая фича
# при таком n, а не сигнал (проверено 2026-07, вернуть при росте выборки)
FEATURES = [
    'difficulty', 'apartments_count', 'floors_max',
    'developer_rating', 'dev_built', 'dev_bad',
    'contractor_rating', 'pod_built', 'pod_bad',
]

BAD_STATUSES = {'stopped', 'frozen', 'cancelled'}


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _log1p(v):
    return None if v is None else float(np.log1p(v))


def connect():
    conn = sqlite3.connect(BASE / 'cam_admin.db')
    conn.row_factory = sqlite3.Row
    return conn


def build_dataset(conn):
    """Собирает DataFrame из БД: и обучающие (target известен), и активные."""
    reg_dates = {r['org_key']: r['reg_date'] for r in conn.execute(
        "SELECT org_key, reg_date FROM org_directory WHERE reg_date IS NOT NULL")}
    # только ОФИЦИАЛЬНЫЙ рейтинг портала (AAA..DDD); ручных оценок нет —
    # оценка риска это выход модели (cam_score), а не входная фича
    ratings_dir = {(r['role'], r['org_key']): r['portal_rating'] for r in conn.execute(
        "SELECT role, org_key, portal_rating FROM org_directory WHERE portal_rating IS NOT NULL")}

    rows = []
    for r in conn.execute('SELECT * FROM complexes'):
        raw = json.loads(r['raw_json']) if r['raw_json'] else {}
        dev_inn = str(r['developer_inn'] or '').strip()
        pod_inn = str(r['contractor_inn'] or '').strip()

        def age_years(inn):
            # возраст на момент старта стройки (а не на сегодня) — иначе
            # у старых сданных объектов возраст завышен и фича смещена
            d = reg_dates.get(inn)
            if not d:
                return None
            ref = (r['created_at'] or '')[:10]
            try:
                ref_d = date.fromisoformat(ref) if ref else date.today()
                return (ref_d - date.fromisoformat(d[:10])).days / 365.25
            except ValueError:
                return None

        # рейтинг: сперва ручной A-D из справочника, иначе старый AAA-DDD
        def rating_of(role, inn, legacy):
            g = ratings_dir.get((role, inn))
            if g:
                return RATING_MAP.get(g)
            return RATING_MAP.get((legacy or '').strip() or None)

        status = r['case_status_clean']
        if status == 'delivered':
            target = int(r['target'] or 0)   # 1 = сдан с большой просрочкой
        elif status in BAD_STATUSES:
            target = 1
        else:
            target = None                    # активный: скорим, не учим

        rows.append({
            'cam_id': r['cam_id'],
            'project_name': r['project_name'],
            'dev_inn': dev_inn or None,
            'pod_inn': pod_inn or None,
            'status': status,
            'target': target,
            'difficulty': DIFF_MAP.get((raw.get('difficulty') or '').strip()),
            # log1p: apartments_count имеет тяжёлый хвост (skew ~8.7, макс. 4720
            # при среднем ~140) — лог сжимает выбросы, помогает LogReg (AUC
            # 0.58->0.64 на valid), на xgboost не влияет (деревья инвариантны
            # к монотонным преобразованиям), не мешает ни одной модели
            'apartments_count': _log1p(_num(raw.get('apartments_count'))),
            'floors_max': _log1p(_num(raw.get('floors_max'))),
            'blocks_total': _num(raw.get('blocks_total')),
            'developer_rating': rating_of('developer', dev_inn, r['developer_rating']),
            'contractor_rating': rating_of('contractor', pod_inn, r['contractor_rating']),
            'dev_age_years': age_years(dev_inn),
            'pod_age_years': age_years(pod_inn),
        })
    return pd.DataFrame(rows)


def add_org_aggregates(df_target, df_source, loo=False):
    """dev_built/dev_bad/pod_built/pod_bad для df_target,
    посчитанные ТОЛЬКО по df_source (train-фолд) — против утечки №2.

    loo=True (когда df_target и есть df_source, т.е. обучение): из счётчиков
    вычитается вклад самого объекта — иначе dev_bad строки содержит её же
    исход, и модель просто запоминает ответ (leave-one-out утечка).
    """
    out = df_target.copy()
    for prefix, inn_col in (('dev', 'dev_inn'), ('pod', 'pod_inn')):
        src = df_source[df_source[inn_col].notna() & df_source['target'].notna()]
        grp = src.groupby(inn_col)['target']
        built = grp.apply(lambda s: int((s == 0).sum()))
        bad = grp.apply(lambda s: int((s == 1).sum()))
        b = out[inn_col].map(built)
        d = out[inn_col].map(bad)
        if loo:
            own = out['target']
            b = b - ((own == 0).astype(int)).where(b.notna(), 0)
            d = d - ((own == 1).astype(int)).where(d.notna(), 0)
            b = b.clip(lower=0)
            d = d.clip(lower=0)
        out[f'{prefix}_built'] = b
        out[f'{prefix}_bad'] = d
    return out


def make_models():
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    models = {
        'logreg_C0.1': Pipeline([
            ('imputer', SimpleImputer(strategy='median')),
            ('scaler', StandardScaler()),
            ('model', LogisticRegression(max_iter=1000, random_state=42,
                                         class_weight='balanced', C=0.1)),
        ]),
        'logreg_C1': Pipeline([
            ('imputer', SimpleImputer(strategy='median')),
            ('scaler', StandardScaler()),
            ('model', LogisticRegression(max_iter=1000, random_state=42,
                                         class_weight='balanced', C=1.0)),
        ]),
    }
    try:
        from xgboost import XGBClassifier
        models['xgboost'] = Pipeline([
            ('imputer', SimpleImputer(strategy='median')),
            ('model', XGBClassifier(
                n_estimators=200, max_depth=3, learning_rate=0.05,
                subsample=0.9, colsample_bytree=0.9,
                eval_metric='auc', random_state=42)),
        ])
    except ImportError:
        pass
    return models


def group_cv_auc(model_factory_name, models, df_train, n_splits=5):
    """GroupKFold по застройщику + фолд-безопасные агрегаты."""
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold

    df = df_train.reset_index(drop=True)
    groups = df['dev_inn'].fillna('NO_INN_' + df['cam_id'])
    gkf = GroupKFold(n_splits=n_splits)
    aucs = []
    for tr_idx, te_idx in gkf.split(df, df['target'], groups):
        tr, te = df.iloc[tr_idx], df.iloc[te_idx]
        tr_f = add_org_aggregates(tr, tr, loo=True)
        te_f = add_org_aggregates(te, tr)    # агрегаты только из train!
        model = make_models()[model_factory_name]
        model.fit(tr_f[FEATURES], tr['target'].astype(int))
        prob = model.predict_proba(te_f[FEATURES])[:, 1]
        if te['target'].nunique() > 1:
            aucs.append(roc_auc_score(te['target'].astype(int), prob))
    return float(np.mean(aucs)), float(np.std(aucs)), len(aucs)


def get_or_create_test_ids(df_train, test_size=0.2, random_state=42):
    """Запертый финальный test (~20% объектов, по ИНН застройщика).

    Список cam_id сохраняется в файл один раз — при повторных train()
    используется тот же test, иначе он перестаёт быть «запертым»
    (пересчёт с новым random_state = скрытый перебор test под удобный результат).
    """
    import json as _json
    if TEST_IDS_PATH.exists():
        ids = set(_json.loads(TEST_IDS_PATH.read_text()))
        present = set(df_train['cam_id'])
        return ids & present  # на случай слияний/удалений карточек

    from sklearn.model_selection import GroupShuffleSplit
    groups = df_train['dev_inn'].fillna('NO_INN_' + df_train['cam_id'])
    gss = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=random_state)
    _, test_idx = next(gss.split(df_train, df_train['target'], groups))
    ids = set(df_train.iloc[test_idx]['cam_id'])
    MODELS_DIR.mkdir(exist_ok=True)
    TEST_IDS_PATH.write_text(_json.dumps(sorted(ids), ensure_ascii=False, indent=2))
    return ids


def evaluate_on_test(model, df_dev, df_test, features):
    """Единственный законный прогон на test. Отдельно считает false negatives —
    случаи «модель сказала хорошо, а на деле плохо» (самые опасные для покупателя)."""
    from sklearn.metrics import roc_auc_score, average_precision_score

    test_f = add_org_aggregates(df_test, df_dev)  # агрегаты только из dev-пула
    prob = model.predict_proba(test_f[features])[:, 1]
    y = df_test['target'].astype(int).values

    out = {'n_test': len(df_test), 'n_bad_test': int(y.sum())}
    if len(set(y)) > 1:
        out['test_auc'] = float(roc_auc_score(y, prob))
        out['test_pr_auc'] = float(average_precision_score(y, prob))
    else:
        out['test_auc'] = None
        out['test_pr_auc'] = None

    # ложноотрицательные по порогам риск-зон (score 0-100, порог 40/70)
    out['thresholds'] = {}
    for thr in (40, 70):
        pred_bad = (prob * 100) >= thr
        fn_mask = (y == 1) & (~pred_bad)          # плохой, но модель сказала «хорошо»
        fp_mask = (y == 0) & pred_bad
        tp = int(((y == 1) & pred_bad).sum())
        fn = int(fn_mask.sum())
        tn = int(((y == 0) & (~pred_bad)).sum())
        fp = int(fp_mask.sum())
        recall_bad = tp / (tp + fn) if (tp + fn) else None
        out['thresholds'][thr] = {
            'tp': tp, 'fn': fn, 'tn': tn, 'fp': fp,
            'recall_bad (меньше => больше пропущенных плохих)': recall_bad,
            'missed_bad_cam_ids': df_test.loc[fn_mask, 'cam_id'].tolist(),
        }
    return out


def train():
    import joblib
    conn = connect()
    df = build_dataset(conn)
    df_train_all = df[df['target'].notna()].copy()
    print(f'Выборка: {len(df_train_all)} объектов с известным исходом '
          f'(target=1: {int(df_train_all.target.sum())}, '
          f'{df_train_all.target.mean():.0%})')
    print(f'Групп-застройщиков: {df_train_all.dev_inn.nunique()}')

    # ── запертый финальный TEST (~20%, по ИНН) — трогаем один раз в самом конце
    test_ids = get_or_create_test_ids(df_train_all)
    df_test = df_train_all[df_train_all['cam_id'].isin(test_ids)].copy()
    df_dev = df_train_all[~df_train_all['cam_id'].isin(test_ids)].copy()
    print(f'TEST заперт: {len(df_test)} объектов ({TEST_IDS_PATH.name}), '
          f'DEV (train+valid): {len(df_dev)} объектов')

    models = make_models()
    results = {}
    print(f'\nВыбор модели на DEV — GroupKFold(5) по ИНН застройщика, ROC-AUC (valid):')
    for name in models:
        mean, std, n = group_cv_auc(name, models, df_dev)
        results[name] = (mean, std)
        print(f'  {name:14} AUC {mean:.4f} ± {std:.4f} ({n} фолдов)')

    best = max(results, key=lambda k: results[k][0])
    print(f'\nЛучшая по valid: {best}')

    # финальное обучение на DEV (test не участвует нигде до этой точки)
    dev_full = add_org_aggregates(df_dev, df_dev, loo=True)
    from sklearn.calibration import CalibratedClassifierCV
    final = CalibratedClassifierCV(make_models()[best], method='isotonic', cv=3)
    final.fit(dev_full[FEATURES], df_dev['target'].astype(int))

    # ── единственный прогон на test ──
    test_report = evaluate_on_test(final, df_dev, df_test, FEATURES)
    print(f"\nTEST (единственный прогон, {test_report['n_test']} объектов, "
          f"{test_report['n_bad_test']} плохих):")
    print(f"  AUC {test_report['test_auc']} | PR-AUC {test_report['test_pr_auc']}")
    for thr, m in test_report['thresholds'].items():
        print(f"  порог {thr}: TP={m['tp']} FN={m['fn']} TN={m['tn']} FP={m['fp']} "
              f"recall_bad={m['recall_bad (меньше => больше пропущенных плохих)']}")
        if m['missed_bad_cam_ids']:
            print(f"    пропущенные плохие (FN): {m['missed_bad_cam_ids']}")

    MODELS_DIR.mkdir(exist_ok=True)
    joblib.dump({
        'model': final, 'model_name': best, 'features': FEATURES,
        'rating_map': RATING_MAP, 'diff_map': DIFF_MAP,
        'cv_auc': results[best][0], 'cv_std': results[best][1],
        'cv_scheme': 'GroupKFold(5) by developer_inn on DEV (test held out)',
        'test_report': test_report,
        'trained_at': date.today().isoformat(),
        'n_train': len(df_dev),
        'n_test': len(df_test),
    }, MODEL_PATH)
    print(f'\nСохранена: {MODEL_PATH} (valid AUC {results[best][0]:.4f}, '
          f"test AUC {test_report['test_auc']})")


def score():
    import joblib
    bundle = joblib.load(MODEL_PATH)
    conn = connect()
    df = build_dataset(conn)
    df_train = df[df['target'].notna()]
    active = df[df['target'].isna()].copy()
    print(f'Модель: {bundle["model_name"]} (CV AUC {bundle["cv_auc"]:.4f}, '
          f'{bundle["trained_at"]})')
    print(f'Активных объектов к скорингу: {len(active)}')

    active_f = add_org_aggregates(active, df_train)
    prob = bundle['model'].predict_proba(active_f[bundle['features']])[:, 1]
    active['cam_score'] = (prob * 100).round(1)
    # Главная граница тревоги — 40, не 70: на test-выборке порог 40 ловит
    # 88% реально плохих объектов (recall), порог 70 — только 44%. Значит
    # "низкий риск" должен значить именно <40, а не <70 — иначе половина
    # плохих ЖК молча попадает в "не тревога" (см. ml_pipeline.py train()).
    active['risk_zone'] = pd.cut(active['cam_score'], [-1, 40, 70, 101],
                                 labels=['low', 'risk', 'high_risk'])

    cols = {r[1] for r in conn.execute('PRAGMA table_info(complexes)')}
    for col, decl in (('cam_score', 'REAL'), ('risk_zone', 'TEXT'),
                      ('scored_at', 'TEXT')):
        if col not in cols:
            conn.execute(f'ALTER TABLE complexes ADD COLUMN {col} {decl}')
    today = date.today().isoformat()
    for _, r in active.iterrows():
        conn.execute('UPDATE complexes SET cam_score=?, risk_zone=?, scored_at=? '
                     'WHERE cam_id=?',
                     (float(r['cam_score']), str(r['risk_zone']), today, r['cam_id']))
    conn.commit()

    print('\nРаспределение CAM Score:')
    print(active['cam_score'].describe().round(1).to_string())
    print('\nРиск-зоны (low <40, risk 40-70, high_risk >=70):')
    print(active['risk_zone'].value_counts().to_string())
    flagged = int((active['cam_score'] >= 40).sum())
    print(f'\nГлавный сигнал тревоги — score >= 40: {flagged} из {len(active)} объектов')
    print('\nТоп-10 риска:')
    top = active.nlargest(10, 'cam_score')[['cam_id', 'project_name', 'cam_score']]
    print(top.to_string(index=False))


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'train'
    if cmd == 'train':
        train()
    elif cmd == 'score':
        score()
    elif cmd == 'dataset':
        conn = connect()
        df = build_dataset(conn)
        print(df[df.target.notna()][FEATURES[:8] + ['target']].describe().round(2))
        print('\nпропуски:')
        print(df[FEATURES[:8]].isna().mean().round(3).to_string())
    else:
        print(__doc__)
