"""Prepare a shared settings patch; never modify a service or place orders."""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server' / 'pulse'))
from connection_profile import ENDPOINTS, connection_endpoint, processing_profile


def prepare(connection, current=None):
    profile = processing_profile()
    current = current or {}
    return dict(connection=connection, endpoint=connection_endpoint(connection),
                activationPerformed=False, profilePatch=profile,
                changes={key:dict(previous=current.get(key), proposed=value)
                         for key,value in profile.items() if current.get(key) != value},
                requiredIndependentState=['credentials', 'open positions', 'pending orders',
                                          'fills', 'STOP marker', 'ownership prefix', 'Redis connection key'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connection', choices=tuple(ENDPOINTS), required=True)
    parser.add_argument('--current-overlay', type=pathlib.Path)
    parser.add_argument('--output-directory', type=pathlib.Path, required=True)
    args = parser.parse_args()
    current = json.loads(args.current_overlay.read_text()) if args.current_overlay else {}
    result = prepare(args.connection, current)
    args.output_directory.mkdir(parents=True, exist_ok=True)
    for filename, value in ((f'{args.connection}-settings-patch.json', result['profilePatch']),
                            (f'{args.connection}-readiness.json', result)):
        (args.output_directory / filename).write_text(json.dumps(value, indent=2)+'\n')
    print(json.dumps(dict(connection=args.connection, changedSettings=len(result['changes']),
                         activationPerformed=False)))


if __name__ == '__main__':
    main()
