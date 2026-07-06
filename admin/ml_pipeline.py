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

# карты из v7
RATING_MAP = {'AAA': 9, 'AA': 8, 'A': 7, 'BBB': 6, 'BB': 5, 'B': 4,
              'CCC': 3, 'CC': 2, 'C': 1, 'DDD': -1, 'DD': -2, 'D': -3}
DIFF_MAP = {'I': 1, 'II': 2, 'III': 3, 'IV': 4}

# возраст компаний исключён: на текущей выборке стабильно ухудшает AUC
# (тест абляции: -0.03), вернуть при росте выборки/покрытия
FEATURES = [
    'difficulty', 'apartments_count', 'floors_max', 'blocks_total',
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


def connect():
    conn = sqlite3.connect(BASE / 'cam_admin.db')
    conn.row_factory = sqlite3.Row
    return conn


def build_dataset(conn):
    """Собирает DataFrame из БД: и обучающие (target известен), и активные."""
    reg_dates = {r['org_key']: r['reg_date'] for r in conn.execute(
        "SELECT org_key, reg_date FROM org_directory WHERE reg_date IS NOT NULL")}
    ratings_dir = {(r['role'], r['org_key']): r['rating'] for r in conn.execute(
        "SELECT role, org_key, rating FROM org_directory WHERE rating IS NOT NULL")}

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
                return {'A': 7, 'B': 4, 'C': 1, 'D': -3}.get(g)
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
            'apartments_count': _num(raw.get('apartments_count')),
            'floors_max': _num(raw.get('floors_max')),
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


def train():
    import joblib
    conn = connect()
    df = build_dataset(conn)
    df_train = df[df['target'].notna()].copy()
    print(f'Выборка: {len(df_train)} объектов с известным исходом '
          f'(target=1: {int(df_train.target.sum())}, '
          f'{df_train.target.mean():.0%})')
    print(f'Групп-застройщиков: {df_train.dev_inn.nunique()}')

    models = make_models()
    results = {}
    print(f'\nВыбор модели — GroupKFold(5) по ИНН застройщика, ROC-AUC:')
    for name in models:
        mean, std, n = group_cv_auc(name, models, df_train)
        results[name] = (mean, std)
        print(f'  {name:14} AUC {mean:.4f} ± {std:.4f} ({n} фолдов)')

    best = max(results, key=lambda k: results[k][0])
    print(f'\nЛучшая: {best}')

    # финальное обучение на всей выборке (агрегаты по всей train-выборке)
    df_full = add_org_aggregates(df_train, df_train, loo=True)
    from sklearn.calibration import CalibratedClassifierCV
    final = CalibratedClassifierCV(make_models()[best], method='isotonic', cv=3)
    final.fit(df_full[FEATURES], df_train['target'].astype(int))

    MODELS_DIR.mkdir(exist_ok=True)
    joblib.dump({
        'model': final, 'model_name': best, 'features': FEATURES,
        'rating_map': RATING_MAP, 'diff_map': DIFF_MAP,
        'cv_auc': results[best][0], 'cv_std': results[best][1],
        'cv_scheme': 'GroupKFold(5) by developer_inn, fold-safe aggregates',
        'trained_at': date.today().isoformat(),
        'n_train': len(df_train),
    }, MODEL_PATH)
    print(f'Сохранена: {MODEL_PATH} (AUC {results[best][0]:.4f})')


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
    active['risk_zone'] = pd.cut(active['cam_score'], [-1, 40, 70, 101],
                                 labels=['low', 'medium', 'high'])

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
    print('\nРиск-зоны:')
    print(active['risk_zone'].value_counts().to_string())
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
