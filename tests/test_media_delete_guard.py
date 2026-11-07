import json
from datetime import date, datetime, timedelta

import pytest
from flask import Flask

from indi_allsky import derived_media, night_movie
from indi_allsky.flask import db
from indi_allsky.flask.models import (
    IndiAllSkyDbCameraTable as Camera, IndiAllSkyDbImageTable as Frame, IndiAllSkyDbVideoTable as Movie,
    IndiAllSkyDbKeogramTable as Keogram, IndiAllSkyDbStateTable as State,
)
from indi_allsky.flask.syncapi_views import SyncApiBaseView
from indi_allsky.media_delete import suppress_flushed_periods, suppress_deleted_local_product


@pytest.fixture
def station(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///:memory:', INDI_ALLSKY_IMAGE_FOLDER=str(tmp_path))
    db.init_app(app)
    today = date(2026, 11, 7)
    monkeypatch.setattr('indi_allsky.utils.IndiAllSkyDateCalcs.getDayDate', lambda self: today)
    with app.app_context():
        db.create_all()
        cams = [Camera(name='test '+str(i), uuid='delete-guard-'+str(i), local=local,
                       latitude=0., longitude=0.) for i, local in enumerate((True, False))]
        db.session.add_all(cams)
        db.session.commit()
        yield cams, today, dict(NIGHT_SUN_ALT_DEG=-6, IMAGE_EXPIRE_DAYS=3, TIMELAPSE_EXPIRE_DAYS=10)


def frame(camera, day, night):
    return Frame(camera_id=camera.id, dayDate=day, night=night,
                 filename=f'{camera.id}-{day}-{night}.jpg', createDate=datetime.now(), exposure=1, gain=1, adu=0)


def test_bulk_flush_covers_inflight_and_source_only_periods_but_not_future_or_expired(station):
    (camera, remote), today, config = station
    day = today - timedelta(days=1)
    frames = [frame(camera, day, night) for night in (True, False)]
    frames += [frame(camera, today + timedelta(days=n), True) for n in (-4, 0, 1)]
    frames.append(frame(remote, day, True))
    old_day = today - timedelta(days=3)
    old_movie = Movie(camera_id=camera.id, dayDate=old_day, night=True, filename='old.mp4')
    running = State(key=night_movie.attempt_key(camera.id, day, True, True),
                    value=json.dumps({'day':day.isoformat(), 'source':'encoder-claim'}))
    db.session.add_all([*frames, old_movie, running])
    db.session.commit()
    claim = running.value
    suppress_flushed_periods(config, camera.id)
    assert not night_movie.lock_repair(camera.id, day, claim, True, True)
    dates_and_modes = [(day, True), (day, False), (old_day, True)]
    expected = {night_movie.attempt_key(camera.id, day, mode, panorama)
                for day, mode in dates_and_modes for panorama in (True, False)}
    expected |= {derived_media.key(camera.id, day, mode) for day, mode in dates_and_modes}
    assert {row.key for row in State.query.all()} == expected
    assert all(json.loads(row.value)['deleted'] for row in State.query.all())


def test_daytime_flush_leaves_night_and_remote_camera_alone(station):
    (camera, remote), today, config = station
    day = today - timedelta(days=2)
    db.session.add_all([frame(camera, day, False), frame(camera, day, True), frame(remote, day, False)])
    db.session.commit()
    suppress_flushed_periods(config, remote.id)
    suppress_flushed_periods(config, camera.id, daytime_only=True)
    assert {row.key for row in State.query.all()} == {
        night_movie.attempt_key(camera.id, day, False), night_movie.attempt_key(camera.id, day, False, True),
        derived_media.key(camera.id, day, False),
    }


def test_explicit_sync_delete_marks_only_selected_local_product(station, tmp_path):
    (camera, remote), today, _ = station
    day = today - timedelta(days=2)
    keep = frame(camera, day, False)
    db.session.add(keep)
    for cam in (camera, remote):
        p = tmp_path / f'{cam.id}-keogram.jpg'
        p.write_bytes(b'old')
        entry = Keogram(camera_id=cam.id, dayDate=day, night=True, filename=str(p))
        db.session.add(entry)
        db.session.commit()
        view = type('SyncKeogram', (), {'model': Keogram})()
        SyncApiBaseView.deleteFile(view, entry.id, cam.id)
        assert not p.exists()
    suppress_deleted_local_product(keep)
    record = State.query.one()
    assert record.key == derived_media.key(camera.id, day, True)
    assert json.loads(record.value)['deleted_kinds'] == ['keogram']
    assert not json.loads(record.value).get('deleted')
