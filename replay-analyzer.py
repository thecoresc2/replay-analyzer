__copyright__ = "(c) 2019 TheCoreSC2 Team <https://github.com/thecoresc2/>"
__author__ = "TheCoreSC2 Team"
__license__ = "MIT, see https://opensource.org/licenses/MIT"

"""
Usage
=====

parse-replays.py <dir>

Where <dir> is a directory containing replay packs (zip files) and/or .SC2Replays.
The directory will be traversed recursively (eg all subdirectories will be browsed).

Any non-replay files will be ignored.

A `results.csv` file will be generated in the current directory.
Setup
=====

Assuming Windows setup.

Install Python from python.org

Make sure you check the "launcher" install.

Open a shell, run `py -3 -m pip install --user pipenv`

`pipenv` is a package manager for python.

Now open or navigate a shell to where the source code is and run:

```
rem Run the program in the environment
> py -3 -m pipenv run replay-analyzer.py -h
```
"""
import argparse
import collections
import csv
import itertools
import logging
import mpyq
import multiprocessing
import operator
import os
import pathlib
import s2protocol
import s2protocol.versions
import sys
import zipfile

EVENT_UNIT_INIT_ID = 6
EVENT_UNIT_BORN_ID = 1
EVENT_CAMERA_SAVE_ID = 14
EVENT_CAMERA_UPDATE_ID = 49
EVENT_PLAYER_SETUP_ID = 9

LATEST = s2protocol.versions.latest()
protocols = {}
WARP_INS = (
  # Gateway warp-ins
  b'Zealot', b'Adept', b'Stalker', b'Sentry', b'DarkTemplar', b'HighTemplar'
)

EXCLUDED_UNITS = (
  b'Larva', b'Broodling', b'BroodlingEscort', b'Archon'
)

def get_protocol(replay):
  # Read the protocol header, this can be read with any protocol
  contents = replay.header['user_data_header']['content']
  header = LATEST.decode_replay_header(contents)
  version = header['m_version']['m_baseBuild']
  if version not in protocols:
    protocols[version] = s2protocol.versions.build(version)
  # Find protocol.
  return protocols[version]


def process_replay(replay, processor):
  return processor.process_replay(mpyq.MPQArchive(replay))

def process_replay_in_pack(pack_and_replay):
  processor, pack, which_replay = pack_and_replay

  try:
    with zipfile.ZipFile(pack) as replay_pack:
      return process_replay(replay_pack.open(which_replay), processor())
  except BaseException as e:
    return {'error': 'Failed to load {} from {}: {}'.format(which_replay, pack, e)}

def process_replay_path(replay_path):
  processor, replay_path = replay_path
  try:
    return process_replay(str(replay_path), processor())
  except BaseException as e:
    return {'error': '{}: {}'.format(str(replay_path), e)}

def count_replays(paths):
  replays_count = 0
  for path in paths:
    # Count files in replay packs.
    for replay_pack in path.glob('**/*.zip'):
      try:
        with zipfile.ZipFile(replay_pack) as pack:
          replays_count += sum(1 for _ in (replay for replay in pack.namelist() if replay.endswith('.SC2Replay')))
      except:
        logging.warning('Failed to open %s. Skipped', replay_pack)
    # Count individual replays
    replays_count += sum(1 for _ in path.glob('**/*.SC2Replay'))
  return replays_count

class BuildProcessor(object):
  def __init__(self):
    self.aggregated = {
      'units': collections.Counter(),
      'buildings': collections.Counter(),
      'abilities': collections.Counter()
    }
  def aggregate(self, replay_stats):
    for k, v in replay_stats.items():
      self.aggregated[k].update(v)

  @classmethod
  def process_replay(cls, replay):
    stats = {
      'units': collections.Counter(),
      'buildings': collections.Counter(),
      'abilities': collections.Counter()
    }

    tracker_events = get_protocol(replay).decode_replay_tracker_events(replay.read_file('replay.tracker.events'))
    # Only born or init events.
    filtered_events = (event for event in tracker_events if event['_eventid'] in (EVENT_UNIT_INIT_ID, EVENT_UNIT_BORN_ID))
    # Only player events.
    filtered_events = (event for event in filtered_events if event['m_controlPlayerId'] > 0 and event['_gameloop'] > 0)
    # Only units of interest.
    filtered_events = (event for event in filtered_events if event['m_unitTypeName'] not in EXCLUDED_UNITS)

    for event in filtered_events:
      unit_name = event['m_unitTypeName']
      eventid = event['_eventid']
      destination = 'units' # By default, assume units.
      creator_ability = None
      if 'm_creatorAbilityName' in event and event['m_creatorAbilityName'] is not None:
        creator_ability = event['m_creatorAbilityName']
      if creator_ability is not None:
        if b'Train' not in creator_ability:
          destination = 'abilities'
          unit_name = b'_'.join([creator_ability, unit_name])  
      elif eventid == EVENT_UNIT_INIT_ID and unit_name not in WARP_INS:
        destination = 'buildings'
        if b'TechLab' in unit_name:
          unit_name = b'TechLab'
        elif b'Reactor' in unit_name:
          unit_name = b'Reactor'
      stats[destination][unit_name.decode('utf-8')] += 1
    return stats

  def write_csv(self, writer):
    writer.writerow(['Type', 'Name', 'Usage Count'])
    for (stuff, count) in self.aggregated['units'].items():
      writer.writerow(['Unit', stuff, count])
    for (stuff, count) in self.aggregated['buildings'].items():
      writer.writerow(['Building', stuff, count])
    for (stuff, count) in self.aggregated['abilities'].items():
      writer.writerow(['Ability', stuff, count])

class CameraProcessor(object):
  def __init__(self):
    self.cameras = {
      'saves': [0] * 8,
      'jumps': [0] * 8
    }
  def aggregate(self, stats):
    for i, (saves, jumps) in enumerate(zip(stats['saves'], stats['jumps'])):
      self.cameras['saves'][i] += saves
      self.cameras['jumps'][i] += jumps

  @classmethod
  def get_playerids(cls, protocol, replay):
    # Details contains the player list with their working ids.
    events = protocol.decode_replay_tracker_events(replay.read_file('replay.tracker.events'))
    # Only player setup events.
    events = (event for event in events if event['_eventid'] == EVENT_PLAYER_SETUP_ID)
    return set((entry['m_userId'] for entry in events))

  @classmethod
  def process_replay(cls, replay):
    # We need to find the players' user ids, which may change from game to game.
    protocol = get_protocol(replay)
    userids = cls.get_playerids(protocol, replay)
    saved_targets = dict(((userid, [None] * 8) for userid in userids))
    saved_cameras = dict(((userid, [0] * 8) for userid in userids))
    jumps = dict(((userid, [0] * 8) for userid in userids))
    events = get_protocol(replay).decode_replay_game_events(replay.read_file('replay.game.events'))
    # Only camera events from players.
    events = (event for event in events if event['_userid']['m_userId'] in userids and event['_eventid'] in (EVENT_CAMERA_SAVE_ID, EVENT_CAMERA_UPDATE_ID))

    for event in events:
      eventid = event['_eventid']
      player = event['_userid']['m_userId']
      if eventid == EVENT_CAMERA_SAVE_ID:
        which = event['m_which']
        # Update the target if necessary
        old_target = saved_targets[player][which]
        if old_target != event['m_target']:
          # Debounce
          saved_targets[player][which] = event['m_target']
          saved_cameras[player][which] += 1
      elif eventid == EVENT_CAMERA_UPDATE_ID:
        if 'm_target' in event:
          target = event['m_target']
          if target in saved_targets[player]:
            # Update jump counter.
            jumps[player][saved_targets[player].index(target)] += 1

    # Merge data sets by cameras
    return {'saves': list(map(operator.add, *saved_cameras.values())), 'jumps': list(map(operator.add, *jumps.values()))}

  def write_csv(self, writer):
    writer.writerow(['Which', 'Saves', 'Jumps'])
    # Write to file.
    for which, (save_count, jump_count) in enumerate(zip(self.cameras['saves'], self.cameras['jumps'])):
      writer.writerow([which, save_count, jump_count])

if __name__ == "__main__":
  multiprocessing.freeze_support()
  common_parser = argparse.ArgumentParser(add_help=False)
  common_parser.add_argument('--output', dest='output', type=str, default='replays.csv', help='Output file (default: replays.csv).')
  common_parser.add_argument('--log', dest='log', type=argparse.FileType('w'), default='replays.log', help='Log file (default: replays.log).')
  common_parser.add_argument('--verbose', dest='verbosity', type=int, default=logging.INFO, help='Log level.')
  common_parser.add_argument('paths', metavar='DIR', type=pathlib.Path, nargs='+', help='Replays folder(s).')
  common_parser.add_argument('--cpus', type=int, default=multiprocessing.cpu_count(), help='Set concurrency (defaults to {})'.format(multiprocessing.cpu_count()))

  parser = argparse.ArgumentParser(description='Mass replay analyzer')
  subparsers = parser.add_subparsers()
  parser_builds = subparsers.add_parser('builds', help='Extract build data', parents=[common_parser])
  parser_builds.set_defaults(processor_class=BuildProcessor)

  parser_cameras = subparsers.add_parser('cameras', help='Extract camera data', parents=[common_parser])
  parser_cameras.set_defaults(processor_class=CameraProcessor)

  args = parser.parse_args()
  logging.basicConfig(stream=args.log, level=args.verbosity)
  logging.info('Looking for replays in: %s', ' '.join((str(path) for path in args.paths)))
  replays_count = count_replays(args.paths)
  logging.info('Processing %d replays', replays_count)

  with open(args.output, 'w', newline='') as csvoutput:
    processed = 0
    processor = args.processor_class()
    with multiprocessing.Pool(args.cpus) as pool:
      for path in args.paths:
        logging.info('Processing replays in %s', path)
        # First, run through archives.
        for replay_pack in path.glob('**/*.zip'):
          logging.info('Processing pack %s', replay_pack)
          try:
            with zipfile.ZipFile(replay_pack) as pack:
              replays = (replay for replay in pack.namelist() if replay.endswith('.SC2Replay'))
              for replay_stats in pool.imap_unordered(process_replay_in_pack, zip(itertools.repeat(args.processor_class), itertools.repeat(replay_pack), (replay for replay in pack.namelist() if replay.endswith('.SC2Replay')))):
                processed += 1
                sys.stdout.write('\b\b\b\b{: >4.0%}'.format(processed / replays_count))
                sys.stdout.flush()
                if 'error' in replay_stats:
                  logging.warning(replay_stats['error'])
                else:
                  processor.aggregate(replay_stats)
          except:
            logging.warning('Failed to open %s. Skipped', replay_pack)
            raise
        # Then run through standalone replays.
        logging.info('Processing standalone replays')
        for replay_stats in pool.imap_unordered(process_replay_path, zip(itertools.repeat(args.processor_class), path.glob('**/*.SC2Replay'))):
          processed += 1
          sys.stdout.write('\b\b\b\b{: >4.0%}'.format(processed / replays_count))
          if 'error' in replay_stats:
            logging.warning(replay_stats['error'])
          else:
            processor.aggregate(replay_stats)
    writer = csv.writer(csvoutput)
    processor.write_csv(writer)

  logging.info('Done')
