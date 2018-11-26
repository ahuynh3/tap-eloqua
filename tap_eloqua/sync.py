import re
import time
import random
from datetime import datetime, timedelta

import pendulum
import singer
from singer import metrics, metadata, Transformer, UNIX_SECONDS_INTEGER_DATETIME_PARSING

from tap_eloqua.schema import (
    BUILT_IN_BULK_OBJECTS,
    ACTIVITY_TYPES,
    get_schemas,
    activity_type_to_stream
)

LOGGER = singer.get_logger()

MIN_RETRY_INTERVAL = 2 # 10 seconds
MAX_RETRY_INTERVAL = 300 # 5 minutes
MAX_RETRY_ELAPSED_TIME = 3600 # 1 hour

def next_sleep_interval(previous_sleep_interval):
    min_interval = previous_sleep_interval or MIN_RETRY_INTERVAL
    max_interval = previous_sleep_interval * 2 or MIN_RETRY_INTERVAL
    return min(MAX_RETRY_INTERVAL, random.randint(min_interval, max_interval))

def get_bookmark(state, stream, default):
    return (
        state
        .get('bookmarks', {})
        .get('visitors', default)
    )

def write_bookmark(state, stream, value):
    if 'bookmarks' not in state:
        state['bookmarks'] = {}
    state['bookmarks'][stream] = value
    singer.write_state(state)

def write_schema(catalog, stream_id):
    stream = catalog.get_stream(stream_id)
    schema = stream.schema.to_dict()
    singer.write_schema(stream_id, schema, stream.key_properties)

def persist_records(catalog, stream_id, records):
    stream = catalog.get_stream(stream_id)
    schema = stream.schema.to_dict()
    stream_metadata = metadata.to_map(stream.metadata)
    with metrics.record_counter(stream_id) as counter:
        for record in records:
            with Transformer(
                integer_datetime_fmt=UNIX_SECONDS_INTEGER_DATETIME_PARSING) as transformer:
                record = transformer.transform(record,
                                               schema,
                                               stream_metadata)
            singer.write_record(stream_id, record)
            counter.increment()

def transform_export_row(row):
    out = {}
    for field, value in row.items():
        if value == '':
            value = None
        out[field] = value
    return out

def stream_export(client, state, catalog, stream_name, sync_id, updated_at_field):
    LOGGER.info('{} - Pulling export results - {}'.format(stream_name, sync_id))

    write_schema(catalog, stream_name)

    limit = 50000
    offset = 0
    has_true = True
    max_updated_at = None
    while has_true:
        data = client.get(
            '/api/bulk/2.0/syncs/{}/data'.format(sync_id),
            params={
                'limit': limit,
                'offset': offset
            },
            endpoint='export_data')
        has_true = data['hasMore']
        offset += limit

        if 'items' in data and data['items']:
            records = map(transform_export_row, data['items'])
            persist_records(catalog, stream_name, records)

            max_page_updated_at = max(map(lambda x: x[updated_at_field], data['items']))
            if max_updated_at is None or max_page_updated_at > max_updated_at:
                max_updated_at = max_page_updated_at

    if max_updated_at:
        write_bookmark(state, stream_name, max_updated_at)

def sync_bulk_obj(client, catalog, state, start_date, stream_name, activity_type=None):
    LOGGER.info('{} - Starting export'.format(stream_name))

    stream = catalog.get_stream(stream_name)

    fields = {}
    obj_meta = None
    for meta in stream.metadata:
        if not meta['breadcrumb']:
            obj_meta = meta['metadata']
        elif meta['metadata'].get('selected', True) or \
             meta['metadata'].get('inclusion', 'available') == 'automatic':
            field_name = meta['breadcrumb'][1]
            fields[field_name] = meta['metadata']['tap-eloqua.statement']

    last_date_raw = get_bookmark(state, stream_name, start_date)
    last_date = pendulum.parse(last_date_raw).to_datetime_string()

    language_obj = obj_meta['tap-eloqua.query-language-name']

    if activity_type:
        updated_at_field = 'CreatedAt'
    else:
        updated_at_field = 'UpdatedAt'

    _filter = "'{{" + language_obj + "." + updated_at_field + "}}' >= '" + last_date + "'"

    if activity_type is not None:
        _filter += " AND '{{Activity.Type}}' = '" + activity_type + "'"

    params = {
        'name': 'Singer Sync - ' + datetime.utcnow().isoformat(),
        'fields': fields,
        'filter': _filter,
        'areSystemTimestampsInUTC': True
    }

    if activity_type:
        url_obj = 'activities'
    elif obj_meta['tap-eloqua.id']:
        url_obj = 'customObjects/' + obj_meta['tap-eloqua.id']
    else:
        url_obj = stream_name

    with metrics.job_timer('bulk_export'):
        data = client.post(
            '/api/bulk/2.0/{}/exports'.format(url_obj),
            json=params,
            endpoint='export_create_def')

        data = client.post(
            '/api/bulk/2.0/syncs',
            json={
                'syncedInstanceUri': data['uri']
            },
            endpoint='export_create_sync')

        sync_id = re.match(r'/syncs/([0-9]+)', data['uri']).groups()[0]

        LOGGER.info('{} - Created export - {}'.format(stream_name, sync_id))

        sleep = 0
        start_time = time.time()
        while True:
            data = client.get(
                '/api/bulk/2.0/syncs/{}'.format(sync_id),
                endpoint='export_sync_poll')

            status = data['status']
            if status == 'success' or status == 'active':
                break
            elif status != 'pending':
                message = '{} - status: {}, exporting failed'.format(
                        stream_name,
                        status)
                LOGGER.error(message)
                raise Exception(message)
            elif (time.time() - start_time) > MAX_RETRY_ELAPSED_TIME:
                message = '{} - export deadline exceeded ({} secs)'.format(
                        stream_name,
                        MAX_RETRY_ELAPSED_TIME)
                LOGGER.error(message)
                raise Exception(message)

            sleep = next_sleep_interval(sleep)
            LOGGER.info('{} - status: {}, sleeping for {} seconds'.format(
                        stream_name,
                        status,
                        sleep))
            time.sleep(sleep)

    stream_export(client,
                  state,
                  catalog,
                  stream_name,
                  sync_id,
                  updated_at_field)

def sync_campaigns(client, catalog, state, start_date):
    write_schema(catalog, 'campaigns')

    last_date_raw = get_bookmark(state, 'campaigns', start_date)
    last_date = pendulum.parse(last_date_raw).to_datetime_string()
    search = "updatedAt>='{}'".format(last_date)

    page = 1
    count = 1000
    while True:
        LOGGER.info('Syncing campaigns since {} - page {}'.format(last_date, page))
        data = client.get(
            '/api/REST/2.0/assets/campaigns',
            params={
                'count': count,
                'page': page,
                'depth': 'complete',
                'orderBy': 'updatedAt',
                'search': search
            },
            endpoint='campaigns')
        page += 1
        records = data.get('elements', [])

        persist_records(catalog, 'campaigns', records)

        if records:
            max_updated_at = pendulum.from_timestamp(
                int(records[-1]['updatedAt'])).to_iso8601_string()
            write_bookmark(state, 'campaigns', max_updated_at)

        if len(records) < count:
            break

def sync_emails(client, catalog, state, start_date):
    write_schema(catalog, 'emails')

    last_date_raw = get_bookmark(state, 'emails', start_date)
    last_date = pendulum.parse(last_date_raw).to_datetime_string()
    search = "updatedAt>='{}'".format(last_date)

    page = 1
    count = 1000
    while True:
        LOGGER.info('Syncing emails since {} - page {}'.format(last_date, page))
        data = client.get(
            '/api/REST/2.0/assets/emails',
            params={
                'count': count,
                'page': page,
                'depth': 'complete',
                'orderBy': 'updatedAt',
                'search': search
            },
            endpoint='emails')
        page += 1
        records = data.get('elements', [])

        persist_records(catalog, 'emails', records)

        if records:
            max_updated_at = pendulum.from_timestamp(
                int(records[-1]['updatedAt'])).to_iso8601_string()
            write_bookmark(state, 'emails', max_updated_at)

        if len(records) < count:
            break

def sync_forms(client, catalog, state, start_date):
    write_schema(catalog, 'forms')

    last_date_raw = get_bookmark(state, 'forms', start_date)
    last_date = pendulum.parse(last_date_raw).to_datetime_string()
    search = "updatedAt>='{}'".format(last_date)

    page = 1
    count = 1000
    while True:
        LOGGER.info('Syncing forms since {} - page {}'.format(last_date, page))
        data = client.get(
            '/api/REST/2.0/assets/forms',
            params={
                'count': count,
                'page': page,
                'depth': 'complete',
                'orderBy': 'updatedAt',
                'search': search
            },
            endpoint='forms')
        page += 1
        records = data.get('elements', [])

        persist_records(catalog, 'forms', records)

        if records:
            max_updated_at = pendulum.from_timestamp(
                int(records[-1]['updatedAt'])).to_iso8601_string()
            write_bookmark(state, 'forms', max_updated_at)

        if len(records) < count:
            break

def sync_visitors(client, catalog, state, start_date):
    write_schema(catalog, 'visitors')

    last_visit_raw = get_bookmark(state, 'visitors', start_date)
    last_visit = pendulum.parse(last_visit_raw).to_datetime_string()
    search = "v_LastVisitDateAndTime>='{}'".format(last_visit)

    page = 1
    count = 1000
    while True:
        LOGGER.info('Syncing visitors since {} - page {}'.format(last_visit, page))
        data = client.get(
            '/api/REST/2.0/data/visitors',
            params={
                'count': count,
                'page': page,
                'depth': 'complete',
                'orderBy': 'v_LastVisitDateAndTime',
                'search': search
            },
            endpoint='visitors')
        page += 1
        records = data.get('elements', [])

        persist_records(catalog, 'visitors', records)

        if records:
            max_visit = pendulum.from_timestamp(
                records[-1]['v_LastVisitDateAndTime']).to_iso8601_string()
            write_bookmark(state, 'visitors', max_visit)

        if len(records) < count:
            break

def get_selected_streams(catalog):
    selected_streams = set()
    for stream in catalog.streams:
        mdata = metadata.to_map(stream.metadata)
        root_metadata = mdata.get(())
        if root_metadata and root_metadata.get('selected') is True:
            selected_streams.add(stream.tap_stream_id)
    return list(selected_streams)

def update_current_stream(state, stream_name):
    state['current_stream'] = stream_name
    singer.write_state(state)

def should_sync_stream(last_stream, selected_streams, stream_name):
    if last_stream == stream_name or \
       (last_stream is None and stream_name in selected_streams):
       return True
    return False

def get_custom_obj_streams(catalog):
    custom_streams = set()
    for stream in catalog.streams:
        mdata = metadata.to_map(stream.metadata)
        root_metadata = mdata.get(())
        if root_metadata and root_metadata.get('tap-eloqua.id'):
            custom_streams.add(stream.tap_stream_id)
    return list(custom_streams)

def sync(client, catalog, state, start_date):
    selected_streams = get_selected_streams(catalog)

    if not selected_streams:
        return

    last_stream = state.get('current_stream')

    for bulk_object in BUILT_IN_BULK_OBJECTS:
        if should_sync_stream(last_stream, selected_streams, bulk_object):
            update_current_stream(state, bulk_object)
            sync_bulk_obj(client,
                          catalog,
                          state,
                          start_date,
                          bulk_object)

    for activity_type in ACTIVITY_TYPES:
        stream_name = activity_type_to_stream(activity_type)
        if should_sync_stream(last_stream, selected_streams, stream_name):
            update_current_stream(state, stream_name)
            sync_bulk_obj(client,
                          catalog,
                          state,
                          start_date,
                          stream_name,
                          activity_type=activity_type)

    for stream_name in get_custom_obj_streams(catalog):
        if should_sync_stream(last_stream, selected_streams, stream_name):
            update_current_stream(state, stream_name)
            sync_bulk_obj(client,
                          catalog,
                          state,
                          start_date,
                          stream_name)


    if should_sync_stream(last_stream, selected_streams, 'visitors'):
        update_current_stream(state, 'visitors')
        sync_visitors(client, catalog, state, start_date)

    if should_sync_stream(last_stream, selected_streams, 'campaigns'):
        update_current_stream(state, 'campaigns')
        sync_campaigns(client, catalog, state, start_date)

    if should_sync_stream(last_stream, selected_streams, 'emails'):
        update_current_stream(state, 'emails')
        sync_emails(client, catalog, state, start_date)

    if should_sync_stream(last_stream, selected_streams, 'forms'):
        update_current_stream(state, 'forms')
        sync_forms(client, catalog, state, start_date)

    update_current_stream(state, None)
