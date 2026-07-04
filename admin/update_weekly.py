"""
Еженедельное обновление данных CAM: одна команда.

    python3 update_weekly.py            # всё, кроме git push
    python3 update_weekly.py --push     # + git commit и push data/

Шаги:
  1. [если задан --csv] импорт свежего парса Shaffof (raw CSV как june_01..04)
  2. пересчёт статистики справочника (объекты, просрочки)
  3. пересборка меток
  4. прогон инспектора (новые находки — в очередь на проверку)
  5. экспорт публичных JSON в ../data/ (сайт обновится после push)
  6. [--push] git add data/ && commit && push

Запускать раз в неделю: вручную или планировщиком
(Windows: Планировщик задач; WSL/Linux: crontab -e →
 0 9 * * 1  cd ~/CAM/admin && /usr/bin/python3 update_weekly.py --push).
"""
import os
import sqlite3
import subprocess
import sys
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)


def log(msg):
    print(f'[update] {msg}', flush=True)


def main():
    push = '--push' in sys.argv
    csv_paths = [a for a in sys.argv[1:] if a.endswith('.csv')]

    conn = sqlite3.connect(os.path.join(BASE, 'cam_admin.db'))
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH '{os.path.join(BASE, 'cam_manual.db')}' AS manual")

    from import_master import build_manual_schema, migrate_complexes
    from org_directory import ensure_schema, ensure_seeded, recompute_stats, rebuild_objects_count
    from tags import rebuild_tags
    import inspector

    build_manual_schema(conn)
    migrate_complexes(conn)
    ensure_schema(conn)
    ensure_seeded(conn)

    # 1. применить свежий парс, если есть CSV за текущий месяц
    #    (сам парс: python3 parse_shaffof.py — 30-60 мин, гоняется отдельно)
    month = date.today().strftime('%Y-%m')
    raw_dir = os.path.join(BASE, 'data', 'raw', month, 'shaffof')
    if os.path.isdir(raw_dir) and any(f.endswith('.csv') for f in os.listdir(raw_dir)):
        import apply_parse
        r = apply_parse.apply(month)
        log(f"парс применён: {r['matched']} карточек, {r['history']} историй, "
            f"{r['promoted']} переведено в «сдан», {r['unmatched']} несовпавших")
    else:
        log(f'свежего парса за {month} нет (data/raw/{month}/shaffof/) — '
            f'пропускаю; для полного цикла сначала: python3 parse_shaffof.py')

    # 2-3. статистика и метки
    recompute_stats(conn)
    rebuild_objects_count(conn)
    rebuild_tags(conn)
    log('статистика справочника и метки пересчитаны')

    # 4. инспектор
    result = inspector.run_checks(conn)
    log(f"инспектор: новых находок {result['new']} (всего сработало {result['total']})")

    # 5. экспорт для сайта
    import export_public
    exp = export_public.export()
    log(f"экспорт: {exp['total']} объектов → data/")

    # 6. push
    if push:
        today = date.today().isoformat()
        subprocess.run(['git', 'add', 'data/'], cwd=REPO, check=True)
        diff = subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=REPO)
        if diff.returncode == 0:
            log('данные не изменились — пушить нечего')
        else:
            subprocess.run(['git', 'commit', '-m', f'Weekly data update {today}'],
                           cwd=REPO, check=True)
            branch = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                                    cwd=REPO, capture_output=True, text=True).stdout.strip()
            subprocess.run(['git', 'push', '-u', 'origin', branch], cwd=REPO, check=True)
            log(f'запушено в {branch} — сайт обновится через пару минут')
    else:
        log('готово (без push; добавьте --push для публикации)')


if __name__ == '__main__':
    main()
