"""Bounded repair of locally recorded media for completed capture dates."""
import hashlib
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import tempfile
import time

from PIL import Image

from .exceptions import TimelapseException
from .media_probe import verified_media

logger = logging.getLogger('indi_allsky')
RECENT_DAYS = 3
OLDER_DATES_PER_SCAN = 2


def attempt_key(camera_id, date, night=True, panorama=False):
    # Date-specific keys preserve independent deletion and retry decisions.
    kind = ('NIGHT' if night else 'DAY') + ('PANO' if panorama else 'MOVIE')
    return '{}_{}_{}'.format(kind, camera_id, date.strftime('%Y%m%d'))


def prior_attempt(state, date):
    if state is None:
        return {}
    try:
        record = json.loads(state.value)
    except (TypeError, ValueError):
        return {}
    return record if isinstance(record, dict) and record.get('day') == date.isoformat() else {}


def _legacy_record(camera_id, date):
    # The previous release used four rotating slots for regular nights. A
    # recorded deletion must remain effective when upgrading the station.
    from .flask import db
    from .flask.models import IndiAllSkyDbStateTable as State
    old = db.session.get(State, 'NIGHTMOVIE_{}_{}'.format(camera_id, date.toordinal() % 4))
    return prior_attempt(old, date)


def suppress_repair(camera_id, date, night=True, panorama=False):
    # A user's explicit delete wins over both scheduled and running repairs.
    from .flask import db
    from .flask.models import IndiAllSkyDbStateTable as State
    key = attempt_key(camera_id, date, night, panorama)
    state = db.session.get(State, key)
    if state is None:
        state = State(key=key)
        db.session.add(state)
    state.value = json.dumps({'day': date.isoformat(), 'deleted': True})
    state.createDate = datetime.now()
    db.session.commit()


def lock_repair(camera_id, date, token, night=True, panorama=False):
    from .flask.models import IndiAllSkyDbStateTable as State
    # Hold the same state row that explicit deletion updates until the movie
    # row is committed. This also works on SQLite without SELECT FOR UPDATE.
    return State.query.filter_by(key=attempt_key(camera_id, date, night, panorama), value=token).update(
        {'value': token}, synchronize_session=False) == 1


def local_sources(entries, skip):
    digest = hashlib.sha256(str(skip).encode('ascii'))
    paths = []
    for entry in entries:
        path = Path(entry.getFilesystemPath())
        try:
            stamp = path.stat()
        except FileNotFoundError:
            continue
        if stamp.st_size:
            digest.update(str((entry.id, str(path), stamp.st_size, stamp.st_mtime_ns)).encode('utf-8'))
            paths.append(path)
    return digest.hexdigest(), paths


def movie_inputs(source_signature, config):
    # A corrected codec/filter or a changed preprocessor is also a new attempt,
    # even if the source files themselves have not changed.
    options = {k: v for k, v in config.items() if k.startswith('FFMPEG_') or k in
               ('TIMELAPSE', 'TIMELAPSE_SKIP_FRAMES', 'IMAGE_FILE_TYPE')}
    data = json.dumps(options, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(source_signature.encode('ascii') + b'\0' + data).hexdigest()


def readable_images(paths):
    for path in paths:
        try:
            with Image.open(path) as image:
                image.load()
        except (OSError, ValueError) as error:
            logger.warning('Skipping unreadable timelapse frame %s: %s', path, error)
            continue
        yield path


def playable(path, ffmpeg='ffmpeg'):
    try:
        if not path.stat().st_size:
            return False
    except FileNotFoundError:
        return False
    # Some corrupt inputs return exit status zero without emitting a frame.
    # One RGB pixel in stdout proves that an actual video frame was decoded.
    try:
        result = subprocess.run([
            ffmpeg, '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-threads', '1', '-i', str(path),
            '-map', '0:v:0', '-frames:v', '1', '-vf', 'scale=1:1', '-pix_fmt', 'rgb24',
            '-c:v', 'rawvideo', '-threads', '1', '-f', 'rawvideo', '-',
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        # A missing/unavailable decoder does not prove that an existing movie is corrupt.
        raise TimelapseException('Could not check timelapse with ffmpeg') from error
    return result.returncode == 0 and len(result.stdout) == 3


@contextmanager
def staged_movie(destination, ffmpeg='ffmpeg'):
    with tempfile.NamedTemporaryFile(prefix='.encode-', suffix=destination.suffix,
                                     dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        yield temporary
        if not playable(temporary, ffmpeg):
            raise TimelapseException('Encoded timelapse contains no decodable video frame')
        # The next database commit publishes this unique filename. Readers of
        # the old row keep their old movie until the replacement is complete.
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _repair_movie(worker, camera, date, night, panorama, frame_model, movie_model):
    from sqlalchemy.exc import IntegrityError
    from .archive_health import archive_paused, DEFERRED
    from .flask import db
    from .flask.models import (IndiAllSkyDbStateTable as State, IndiAllSkyDbTaskQueueTable as Task,
                               TaskQueueQueue, TaskQueueState)

    movies = movie_model.query.filter_by(camera_id=camera.id, dayDate=date, night=night).all()
    # Remote-only assets and ambiguous duplicate rows need operator review.
    if len(movies) > 1 or any(movie.remote_url or movie.s3_key for movie in movies):
        return False
    if movies:
        path = Path(movies[0].getFilesystemPath())
        if verified_media(path, playable):
            if not movies[0].success:
                movies[0].success = True
                movies[0].fileSize = path.stat().st_size
                db.session.commit()
            return False
    key = attempt_key(camera.id, date, night, panorama)
    state = db.session.get(State, key)
    prior = prior_attempt(state, date)
    if not prior and night and not panorama:
        prior = _legacy_record(camera.id, date)
    if prior.get('deleted'):
        return False

    entries = frame_model.query.filter_by(camera_id=camera.id, dayDate=date, night=night, exclude=False)
    skip = worker.config.get('TIMELAPSE_SKIP_FRAMES', 4)
    signature, sources = local_sources(entries.order_by(frame_model.id), skip)
    signature = movie_inputs(signature, worker.config)
    if not movies and not sources and not entries.first():
        return False  # there is no evidence this camera recorded this product
    failures = 0
    if prior.get('source') == signature and prior.get('result'):
        # A normal failure is final until the sources change. Only an interrupted
        # encode gets one extra attempt once the supervisor expires its task.
        previous = db.session.get(Task, prior['task_id']) if prior.get('task_id') else None
        failures = prior.get('attempt', 1)
        interrupted = (previous.state == TaskQueueState.EXPIRED if previous else
                       'repair started;' in prior.get('result', ''))
        elapsed = time.time() - prior.get('started_at', 0)
        if not interrupted or failures >= 2:
            return False
        if 0 <= elapsed < 120:
            return True  # recheck in a minute, after the short restart cooldown
    if archive_paused(worker.config):
        return False

    action = 'generatePanoramaVideo' if panorama else 'generateVideo'
    label = ('Night' if night else 'Day') + (' panorama' if panorama else ' timelapse')
    args = dict(camera_id=camera.id, timespec=date.strftime('%Y%m%d'), night=night, repair=True)
    task = Task(queue=TaskQueueQueue.VIDEO, state=TaskQueueState.RUNNING,
                data={'action': action, 'kwargs': args})
    db.session.add(task)
    db.session.flush()
    record = {'day': date.isoformat(), 'source': signature, 'task_id': task.id, 'attempt': failures + 1,
              'started_at': int(time.time()),
              'result': '{} repair started; no completed replacement has been published.'.format(label)}
    token = json.dumps(record)
    if state is None:
        db.session.add(State(key=key, value=token))
    else:
        changed = State.query.filter_by(key=key, value=state.value).update(
            {'value': token, 'createDate': datetime.now()}, synchronize_session=False)
        if not changed:
            db.session.rollback()
            return False
    try:
        db.session.commit()  # reserve the attempt before encoding
    except IntegrityError:
        db.session.rollback()
        return False
    try:
        if len(sources) <= skip:
            task.setFailed('{} {}: insufficient local source images ({} available, {} skipped).'.format(
                label, date, len(sources), skip))
        else:
            getattr(worker, action)(task, repair_token=token, **args)
    except Exception:
        logger.exception('%s repair failed for %s', label, date)
        db.session.rollback()
        # Publication commits before export. Do not downgrade an already-published movie.
        if task.state != TaskQueueState.SUCCESS:
            task.setFailed('{} {}: repair failed; inspect the video worker log.'.format(label, date))
    record['result'] = '' if task.state == TaskQueueState.SUCCESS else task.result
    if record['result'] and (record['result'] == DEFERRED or archive_paused(worker.config)):
        record.pop('source')  # storage outages do not consume an encoding attempt
    State.query.filter_by(key=key, value=token).update({'value': json.dumps(record)}, synchronize_session=False)
    db.session.commit()
    return len(sources) > skip


def reconcile(worker):
    """Process at most one expensive product. Return true to revisit a backlog soon."""
    from sqlalchemy.orm.exc import NoResultFound
    from .archive_health import archive_paused
    from .flask.models import (IndiAllSkyDbCameraTable as Camera, IndiAllSkyDbImageTable as Frame,
        IndiAllSkyDbVideoTable as Movie, IndiAllSkyDbPanoramaImageTable as PanoFrame,
        IndiAllSkyDbPanoramaVideoTable as PanoMovie, IndiAllSkyDbTaskQueueTable as Task,
        TaskQueueQueue, TaskQueueState)
    from .utils import IndiAllSkyDateCalcs
    from . import derived_media

    config = worker.config
    if (not config.get('TIMELAPSE_ENABLE', True) or config.get('CAPTURE_PAUSE')
            or config.get('FOCUS_MODE') or archive_paused(config)):
        return False
    try:
        camera_id = int(worker._miscDb.getState('DB_CAMERA_ID'))
    except (NoResultFound, ValueError, TypeError):
        return False
    camera = Camera.query.filter_by(id=camera_id, local=True, hidden=False).first()
    if not camera or camera.latitude is None or camera.longitude is None:
        return False
    today = IndiAllSkyDateCalcs(config, [camera.latitude, camera.longitude, camera.elevation or 0]).getDayDate()
    days = min(config.get('IMAGE_EXPIRE_DAYS', 10), config.get('TIMELAPSE_EXPIRE_DAYS', 365))
    if days <= 0:
        return False
    cutoff = today - timedelta(days=days)
    outstanding = set()
    for task in Task.query.filter(Task.queue == TaskQueueQueue.VIDEO, Task.state.in_(
            (TaskQueueState.QUEUED, TaskQueueState.RUNNING, TaskQueueState.MANUAL))):
        data = task.data or {}
        args = data.get('kwargs', {})
        if args.get('camera_id') == camera.id:
            outstanding.add((data.get('action'), args.get('timespec'), bool(args.get('night'))))

    day_enabled = (config.get('DAYTIME_CAPTURE', True) and config.get('DAYTIME_CAPTURE_SAVE', True)
                   and config.get('DAYTIME_TIMELAPSE', True))
    pano_enabled = config.get('FISH2PANO', {}).get('ENABLE', False)
    enabled = [False] + ([True] if pano_enabled else [])
    checked = set()
    def check(date, night, panorama):
        identity = (date, night, panorama)
        if identity in checked or (not night and not day_enabled):
            return False
        checked.add(identity)
        action = 'generatePanoramaVideo' if panorama else 'generateVideo'
        if (action, date.strftime('%Y%m%d'), night) in outstanding:
            return False
        if worker._shutdown or archive_paused(config):
            return False
        return _repair_movie(worker, camera, date, night, panorama,
                             PanoFrame if panorama else Frame, PanoMovie if panorama else Movie)

    # Rows already marked failed are actionable and retain public watch URLs.
    # Bound this fast path; the rotating date scan also reaches older rows.
    for panorama in reversed(enabled):
        model = PanoMovie if panorama else Movie
        candidates = model.query.filter_by(camera_id=camera.id, success=False).filter(
            model.dayDate >= cutoff, model.dayDate < today).order_by(model.dayDate.desc()).limit(2)
        for candidate in candidates:
            if check(candidate.dayDate, candidate.night, panorama):
                return True

    def check_period(date):
        for night in (True, False) if day_enabled else (True,):
            for panorama in enabled:
                if check(date, night, panorama):
                    return True
            if not worker._shutdown and not archive_paused(config) and derived_media.reconcile(worker, camera, date, night):
                return True
        return False

    for offset in range(1, min(RECENT_DAYS, days) + 1):
        if check_period(today - timedelta(days=offset)):
            return True
    if days <= RECENT_DAYS or worker._shutdown:
        return False

    # Rotate through at most two older dates with known sources or movies per
    # pass. An interrupted long-running station can catch up within retention
    # without repeatedly scanning every image directory on every timer tick.
    from .flask import db
    from .flask.models import IndiAllSkyDbStateTable as State
    cursor_key = 'MEDIASCAN_' + str(camera.id)
    cursor = db.session.get(State, cursor_key)
    end = today - timedelta(days=RECENT_DAYS)
    def older_dates(before):
        dates = set()
        for model in ((Movie, Frame, PanoMovie, PanoFrame) if pano_enabled else (Movie, Frame)):
            q = model.query.with_entities(model.dayDate).filter(model.camera_id == camera.id,
                    model.dayDate >= cutoff, model.dayDate < before)
            if not day_enabled:
                q = q.filter(model.night.is_(True))
            dates.update(row[0] for row in q.distinct().order_by(model.dayDate.desc()).limit(OLDER_DATES_PER_SCAN))
        return sorted(dates, reverse=True)[:OLDER_DATES_PER_SCAN]
    before = end
    if cursor is not None:
        try:
            before = min(end, datetime.strptime(cursor.value, '%Y%m%d').date())
        except ValueError:
            pass
    older = older_dates(before)
    if not older and before != end:
        older = older_dates(end)
    for date in older:
        if cursor is None:
            cursor = State(key=cursor_key)
            db.session.add(cursor)
        cursor.value = date.strftime('%Y%m%d')
        cursor.createDate = datetime.now()
        db.session.commit()
        if check_period(date):
            return True
    return False
