#!/user/bin/env python3
from __future__ import division
import sys
import os
from datetime import datetime
from multiprocessing import Pool
import logging
import warnings
from datetime import timedelta
from collections import Counter, namedtuple
from itertools import chain, compress, cycle
import numpy as np
import pytz
import psycopg2
from psycopg2.extras import NamedTupleCursor

'''
Goal: from bustime positions, impute stop calls. Each output row should contain:
vehicle_id, trip_index, stop_sequence, arrival_time, departure_time, source (??).
'''

logger = logging.getLogger()
logger.setLevel(logging.INFO)
loghandler = logging.StreamHandler(sys.stdout)
logformatter = logging.Formatter(fmt='%(levelname)s: %(message)s')
loghandler.setFormatter(logformatter)
logger.addHandler(loghandler)

warnings.simplefilter('ignore', np.RankWarning)

# Maximum elapsed time between positions before we declare a new run
MAX_TIME_BETWEEN_STOPS = timedelta(seconds=60 * 30)

# when dist_from_stop < 30.48 m (100 feet) considered "at stop" by MTA --NJ
# this is not correct! It's only that the sign displays "at stop"
# beginning at 100 ft. Nevertheless, we're doing 100 ft
STOP_THRESHOLD = 30.48

# Purposefully ignoring daylight savings for now
VEHICLE_QUERY = """SELECT
    timestamp_utc AS timestamp,
    vehicle_id,
    p.trip_id,
    service_date,
    next_stop_id next_stop,
    sdas.stop_sequence seq,
    dist_along_route,
    dist_along_shape,
    dist_from_stop,
    dist_along_shape - dist_from_stop AS distance
FROM positions p
    LEFT JOIN gtfs_trips t USING (trip_id)
    LEFT JOIN gtfs_stop_times st ON (p.trip_id = st.trip_id AND p.next_stop_id::text = st.stop_id)
    INNER JOIN gtfs_stop_distances_along_shape sdas ON (t.shape_id = sdas.shape_id AND p.next_stop_id::integer = sdas.stop_id::integer)
WHERE
    vehicle_id = %s
    AND (
        service_date = %s
        OR (
            DATE(timestamp_utc::TIMESTAMP WITH TIME ZONE AT TIME ZONE 'EST') = DATE %s - INTERVAL '1 DAY'
            AND EXTRACT(HOUR FROM timestamp_utc::TIMESTAMP WITH TIME ZONE AT TIME ZONE 'EST') > 23
        )
        OR (
            DATE(timestamp_utc::TIMESTAMP WITH TIME ZONE AT TIME ZONE 'EST') = DATE %s + INTERVAL '1 DAY'
            AND EXTRACT(HOUR FROM timestamp_utc::TIMESTAMP WITH TIME ZONE AT TIME ZONE 'EST') < 4
        )
    )
ORDER BY trip_id, st.stop_sequence, timestamp_utc;
"""

SELECT_VEHICLE = """SELECT DISTINCT vehicle_id
    FROM positions WHERE service_date = %s"""

SELECT_TRIP_INDEX = """SELECT
    st.stop_id id,
    arrival_time AS time,
    t.route_id,
    t.direction_id,
    stop_id,
    st.stop_sequence AS seq,
    sdas.dist_along_shape
FROM gtfs_trips t
    LEFT JOIN gtfs_stop_times st USING (trip_id)
    LEFT JOIN gtfs_stops s USING (stop_id)
    LEFT JOIN gtfs_stop_distances_along_shape sdas USING (shape_id, stop_id)
WHERE trip_id = %s
ORDER BY st.stop_sequence ASC
"""

INSERT = """INSERT INTO {}
    (vehicle_id, trip_id, route_id, direction_id, stop_id, call_time, source)
    VALUES ({}, '{}', %s, %s, %s, %s, %s)"""

EPOCH = datetime.utcfromtimestamp(0)
EST = pytz.timezone('US/Eastern')

Call = namedtuple('Call', ['route_id', 'direction_id', 'stop_id', 'timestamp', 'method'])


def to_unix(dt):
    return (dt - EPOCH).total_seconds()


def common(lis):
    return Counter(lis).most_common(1)[0][0]


def mask(positions, key):
    filt = (key(x, y) for x, y in zip(positions[1:], positions))
    ch = chain([True], filt)
    return list(compress(positions, ch))


def filter_positions(cursor, date):
    '''
    Compile list of positions for a vehicle, using a list of positions
    and filtering based on positions that reflect change in pattern or next_stop.
    Generates a list of preliminary information:
        vehicle
        trip index
        stop sequence
        arrival min
        arrival max
        departure min
        departure max
    '''
    runs = []
    prev = object()
    position = cursor.fetchone()

    while position is not None:
        # If patterns differ, stop sequence goes down, or half an hour passed
        if (position.trip_id != getattr(prev, 'trip_id', None) or
                (position.seq or -2) < getattr(prev, 'seq', -1)):
            # start a new run
            runs.append([])

        # append the current stop
        runs[-1].append(position)
        prev = position
        position = cursor.fetchone()

    # filter out any runs that start the next day
    # mask runs to eliminate out-of-order stop sequences
    runs = [mask(run, lambda x, y: x.seq >= y.seq) for run in runs
            if run[0].service_date.isoformat() == date
            ]

    return runs


def call(stoptime, seconds, method=None):
    return Call(
        stoptime.route_id,
        stoptime.direction_id,
        stoptime.stop_id,
        datetime.utcfromtimestamp(seconds).replace(tzinfo=pytz.UTC).astimezone(EST),
        method or 'I'
    )


def generate_calls(run, stoptimes):
    '''
    list of calls to be written
    Args:
        run: list generated from enumerate(positions)
        stoptimes: list of scheduled stoptimes for this trip
    '''
    # each call is a list of this format:
    # [route, direction, stop, stop_sequence, datetime, source]
    obs_distances = [p.distance for p in run]
    obs_times = [to_unix(p.timestamp) for p in run]

    # purposefully avoid the first and last stops
    stop_positions = [x.dist_along_shape for x in stoptimes]

    # set start index to the stop that first position (P.0) is approaching
    try:
        si = stoptimes.index([x for x in stoptimes if x.seq == run[0].seq][0])
    except (IndexError, AttributeError):
        si = 0

    # set end index to the stop approached by the last position (P.n) (which means it won't be used in interp)
    try:
        ei = stoptimes.index([x for x in stoptimes if x.seq == run[-1].seq][0])
    except (AttributeError, IndexError):
        ei = len(stoptimes)

    interpolated = np.interp(stop_positions[si:ei], obs_distances, obs_times)
    calls = [call(stop, secs) for stop, secs in zip(stoptimes[si:ei], interpolated)]

    if len(calls) == 0:
        return []

    if len(run) > 3:
        # Extrapolate forward to the next stop after the positions
        if ei < len(stoptimes):
            coefficients = np.polyfit(obs_distances[-3:], obs_times[-3:], 1)
            try:
                extrapolated = np.poly1d(coefficients)(stop_positions[ei])
                calls.append(call(stoptimes[ei], extrapolated, 'E'))
            except (ValueError, TypeError):
                pass

        # Extrapolate back for a single stop before the positions
        if si > 0:
            coefficients = np.polyfit(obs_distances[:3], obs_times[:3], 1)
            try:
                extrapolated = np.poly1d(coefficients)(stop_positions[si])
                calls.insert(0, call(stoptimes[si], extrapolated, 'S'))
            except ValueError:
                pass
            except TypeError:
                print(run[0])
                print(si)
                print(coefficients)
                print(stop_positions[:si])

    return calls


def process_vehicle(vehicle_id, table, date, connectionstring):
    with psycopg2.connect(connectionstring, cursor_factory=NamedTupleCursor) as conn:
        print('STARTING', vehicle_id, file=sys.stderr)
        with conn.cursor() as cursor:
            # load up cursor with every position for vehicle
            cursor.execute(VEHICLE_QUERY, (vehicle_id, date, date, date))
            runs = filter_positions(cursor, date)
            lenc = 0

            # each run will become a trip
            for run in runs:
                if len(run) == 0:
                    continue
                elif len(run) <= 2:
                    logging.info('short run (%d positions), v_id=%s, %s',
                                 len(run), vehicle_id, run[0].timestamp)
                    continue

                # get the scheduled list of trips for this run
                trip_id = common([x.trip_id for x in run])

                cursor.execute(SELECT_TRIP_INDEX, (trip_id,))
                calls = generate_calls(run, cursor.fetchall())

                # write calls to sink
                insert = INSERT.format(table, vehicle_id, trip_id)
                cursor.executemany(insert, calls)
                lenc += len(calls)
                conn.commit()

            print('COMMIT', vehicle_id, lenc, file=sys.stderr)


def main(connectionstring, table, date, vehicle=None):
    # connect to MySQL

    if vehicle:
        vehicles = [vehicle]
    else:
        with psycopg2.connect(connectionstring) as conn:
            with conn.cursor() as cursor:
                cursor.execute(SELECT_VEHICLE, (date,))
                vehicles = [x[0] for x in cursor.fetchall()]

    itervehicles = zip(vehicles,
                       cycle([table]),
                       cycle([date]),
                       cycle([connectionstring])
                       )

    with Pool(os.cpu_count()) as pool:
        pool.starmap(process_vehicle, itervehicles)

    print("SUCCESS: Committed %s" % date, file=sys.stderr)


if __name__ == '__main__':
    main(*sys.argv[1:])
