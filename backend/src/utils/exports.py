import csv
from src.config.settings import ARTIFACTS


def safe_cell(value):
    if isinstance(value, str) and (value.lstrip().startswith(('=', '+', '-', '@'))
                                   or value.startswith(('\t', '\r', '\n'))):
        return "'" + value
    return value


def render_rows(result):
    """与页面结果共用同一份载荷，保证底稿与桥接、渠道处置同源。"""
    rows = [
        [safe_cell(result['headline'])],
        [safe_cell(result.get('note', ''))],
    ]
    for metric in result['metrics']:
        rows.append([safe_cell(metric['label']), safe_cell(metric['value']), safe_cell(metric.get('detail', ''))])
    for table in result['tables']:
        rows.append([])
        rows.append([safe_cell(table['title'])])
        columns = table['columns']
        rows.append([safe_cell(column['label']) for column in columns])
        for row in table['rows']:
            rows.append([safe_cell(row.get(column['key'])) for column in columns])
    snapshot = result.get('snapshot') or {}
    rows.append([])
    rows.append(['快照营业日', safe_cell(snapshot.get('report_day', ''))])
    rows.append(['快照行数', safe_cell(snapshot.get('row_count', ''))])
    rows.append(['快照指纹', safe_cell(snapshot.get('ledger_fingerprint', ''))])
    rows.append(['结果指纹', safe_cell(result.get('fingerprint', ''))])
    return rows


def write_report(run_id, attempt, result):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    name = f'analysis-{run_id}-{attempt}.csv'
    path = ARTIFACTS / name
    staging = path.with_suffix('.tmp')
    with staging.open('w', encoding='utf-8-sig', newline='') as output:
        writer = csv.writer(output)
        writer.writerows(render_rows(result))
    staging.replace(path)
    return name
