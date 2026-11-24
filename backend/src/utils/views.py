from src.utils.time import iso


def dataset(item):
    return {'id': item.id, 'name': item.name, 'filename': item.filename, 'row_count': item.row_count,
            'checksum': item.checksum, 'author': item.author, 'created_at': iso(item.created_at)}


def publication(item):
    if item is None:
        return None
    return {
        'id': item.id, 'report_day': item.report_day, 'version': item.version,
        'run_id': item.run_id, 'dataset_id': item.dataset_id,
        'dataset_checksum': item.dataset_checksum, 'fingerprint': item.fingerprint,
        'state': item.state, 'export_file': item.export_file,
        'published_at': iso(item.published_at), 'superseded_at': iso(item.superseded_at),
    }


def run(item, detail=False):
    item_view = {'id': item.id, 'dataset_id': item.dataset_id, 'name': item.name, 'status': item.status,
                 'author': item.author, 'review': item.review, 'decision': item.decision,
                 'created_at': iso(item.created_at), 'finished_at': iso(item.finished_at),
                 'scheduled_at': iso(item.scheduled_at), 'schedule_id': item.schedule_id,
                 'has_export': bool(item.export_file), 'error': item.error,
                 'publication_id': item.publication_id,
                 'report_day': (item.result or {}).get('snapshot', {}).get('report_day')
                 if item.result else item.params.get('report_day')}
    if detail:
        item_view.update(params=item.params, result=item.result, review_note=item.review_note)
    return item_view


def schedule(item):
    from src.domain.spec import SPEC
    return {'id': item.id, 'name': item.name, 'dataset_id': item.dataset_id, 'recipient': item.recipient,
            'next_at': iso(item.next_at), 'repeat_daily': item.repeat_daily, 'enabled': item.enabled,
            'next_report_day': item.params.get(SPEC['schedule_date_param']) if item.enabled else None}


def delivery(item):
    return {'id': item.id, 'run_id': item.run_id, 'recipient': item.recipient,
            'state': item.state, 'detail': item.detail, 'created_at': iso(item.created_at)}
