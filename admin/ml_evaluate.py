"""
Полная оценка пайплайна по продакшен-метрикам.

Всё меряется на out-of-fold предсказаниях GroupKFold по ИНН застройщика
(объект оценивается моделью, которая его застройщика не видела):

  1. Дискриминация: ROC-AUC, PR-AUC (vs доля позитивов), KS
  2. Бейзлайны: константа, «только рейтинг застройщика», «только dev_bad>0»
  3. Калибровка: Brier score + таблица надёжности (сказали 80% — сбывается ли в 80%?)
  4. Рабочие пороги (зоны 40/70): precision/recall/алерты на каждом
  5. Стабильность: AUC по фолдам (разброс = насколько верить средней цифре)
  6. Абляция: вклад каждой группы фич (что выкинуть — что добавить)

Запуск: python3 ml_evaluate.py
"""
import numpy as np
import pandas as pd

from ml_pipeline import (FEATURES, add_org_aggregates, build_dataset,
                         connect, get_or_create_test_ids, make_models)

FEATURE_GROUPS = {
    'объект (сложность/размер)': ['difficulty', 'apartments_count', 'floors_max'],
    'рейтинги':                  ['developer_rating', 'contractor_rating'],
    'возраст компаний':          ['dev_age_years', 'pod_age_years'],
    'история портфеля':          ['dev_built', 'dev_bad', 'pod_built', 'pod_bad'],
}


def oof_predictions(model_name, df_train, features=None, n_splits=5):
    """Out-of-fold вероятности: каждый объект предсказан моделью,
    не видевшей его застройщика."""
    from sklearn.model_selection import GroupKFold
    features = features or FEATURES
    df = df_train.reset_index(drop=True)
    groups = df['dev_inn'].fillna('NO_INN_' + df['cam_id'])
    oof = np.full(len(df), np.nan)
    for tr_idx, te_idx in GroupKFold(n_splits=n_splits).split(df, df['target'], groups):
        tr, te = df.iloc[tr_idx], df.iloc[te_idx]
        tr_f = add_org_aggregates(tr, tr, loo=True)
        te_f = add_org_aggregates(te, tr)
        model = make_models()[model_name]
        model.fit(tr_f[features], tr['target'].astype(int))
        oof[te_idx] = model.predict_proba(te_f[features])[:, 1]
    return df['target'].astype(int).values, oof


def fold_aucs(model_name, df_train, features=None, n_splits=5):
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold
    features = features or FEATURES
    df = df_train.reset_index(drop=True)
    groups = df['dev_inn'].fillna('NO_INN_' + df['cam_id'])
    out = []
    for tr_idx, te_idx in GroupKFold(n_splits=n_splits).split(df, df['target'], groups):
        tr, te = df.iloc[tr_idx], df.iloc[te_idx]
        tr_f = add_org_aggregates(tr, tr, loo=True)
        te_f = add_org_aggregates(te, tr)
        model = make_models()[model_name]
        model.fit(tr_f[features], tr['target'].astype(int))
        p = model.predict_proba(te_f[features])[:, 1]
        if te['target'].nunique() > 1:
            out.append(roc_auc_score(te['target'].astype(int), p))
    return out


def ks_stat(y, p):
    from scipy.stats import ks_2samp
    return ks_2samp(p[y == 1], p[y == 0]).statistic


def calibration_table(y, p, bins=5):
    df = pd.DataFrame({'y': y, 'p': p})
    df['bin'] = pd.qcut(df['p'], bins, duplicates='drop')
    g = df.groupby('bin', observed=True).agg(
        предсказано=('p', 'mean'), фактически=('y', 'mean'), объектов=('y', 'size'))
    return (g * [100, 100, 1]).round(0).astype(int)


def threshold_metrics(y, p, thr):
    pred = (p * 100 >= thr).astype(int)
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    return {'порог': thr, 'алертов': tp + fp,
            'точность': f'{precision:.0%}', 'полнота': f'{recall:.0%}'}


def main():
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

    conn = connect()
    df = build_dataset(conn)
    df_train_all = df[df['target'].notna()].copy()
    # Всё ниже — это valid (GroupKFold), не финальная цифра: тест заперт
    # в models/test_ids_v8.json и меряется один раз в ml_pipeline.py train().
    # Если гонять этот скрипт на всех 259 объектах, тест тихо утекает в
    # каждую метрику здесь — тогда "финальных" чисел стало бы два разных.
    test_ids = get_or_create_test_ids(df_train_all)
    df_train = df_train_all[~df_train_all['cam_id'].isin(test_ids)].copy()
    y_rate = df_train['target'].mean()
    print(f'DEV-выборка (valid, test заперт отдельно): {len(df_train)} из {len(df_train_all)} '
          f'| доля плохих исходов: {y_rate:.0%} | групп-застройщиков: {df_train.dev_inn.nunique()}')

    # ── 1-2. модели vs бейзлайны ─────────────────────────────────────────
    print('\n══ 1. Модели против бейзлайнов (out-of-fold, GroupKFold по ИНН) ══')
    rows = []
    oof_cache = {}
    for name in make_models():
        y, p = oof_predictions(name, df_train)
        mask = ~np.isnan(p)
        y, p = y[mask], p[mask]
        oof_cache[name] = (y, p)
        rows.append({'модель': name,
                     'ROC-AUC': round(roc_auc_score(y, p), 3),
                     'PR-AUC': round(average_precision_score(y, p), 3),
                     'KS': round(ks_stat(y, p), 3),
                     'Brier': round(brier_score_loss(y, p), 3)})

    # бейзлайн: только рейтинг застройщика (чем ниже, тем хуже)
    br = df_train.dropna(subset=['developer_rating'])
    if br['target'].nunique() > 1:
        auc_r = roc_auc_score(br['target'].astype(int), -br['developer_rating'])
        rows.append({'модель': f'бейзлайн: только рейтинг ({len(br)} объектов)',
                     'ROC-AUC': round(auc_r, 3), 'PR-AUC': '—', 'KS': '—', 'Brier': '—'})
    rows.append({'модель': 'бейзлайн: константа (все = доля плохих)',
                 'ROC-AUC': 0.5, 'PR-AUC': round(y_rate, 3), 'KS': 0.0,
                 'Brier': round(y_rate * (1 - y_rate), 3)})
    print(pd.DataFrame(rows).to_string(index=False))

    best = max(oof_cache, key=lambda k: roc_auc_score(*oof_cache[k]))
    y, p = oof_cache[best]
    print(f'\nЛучшая: {best}')

    # ── 3. калибровка ────────────────────────────────────────────────────
    print('\n══ 2. Калибровка (можно ли верить цифре скора) ══')
    print(calibration_table(y, p).to_string())

    # ── 4. рабочие пороги ────────────────────────────────────────────────
    print('\n══ 3. Рабочие пороги риск-зон ══')
    print(pd.DataFrame([threshold_metrics(y, p, t) for t in (40, 70)]).to_string(index=False))
    print('  точность = сколько алертов реально плохие; полнота = сколько плохих поймали')

    # ── 5. стабильность ──────────────────────────────────────────────────
    print('\n══ 4. Стабильность по фолдам ══')
    fa = fold_aucs(best, df_train)
    print(f'  AUC фолдов: {[round(a, 3) for a in fa]}')
    print(f'  среднее {np.mean(fa):.3f} ± {np.std(fa):.3f} '
          f'(разброс {min(fa):.3f}–{max(fa):.3f})')

    # ── 6. абляция ───────────────────────────────────────────────────────
    print('\n══ 5. Вклад групп фич (абляция: минус группа → изменение AUC) ══')
    base_auc = np.mean(fold_aucs(best, df_train))
    for gname, gcols in FEATURE_GROUPS.items():
        subset = [f for f in FEATURES if f not in gcols]
        auc_wo = np.mean(fold_aucs(best, df_train, features=subset))
        delta = base_auc - auc_wo
        mark = '▲ важна' if delta > 0.01 else ('▼ шумит' if delta < -0.01 else '≈ нейтральна')
        print(f'  без «{gname}»: AUC {auc_wo:.3f} (Δ {delta:+.3f}) {mark}')

    print(f'\nБазовый AUC со всеми фичами: {base_auc:.3f}')


if __name__ == '__main__':
    main()
