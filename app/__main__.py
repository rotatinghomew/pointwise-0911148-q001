TITLE = '时段开关试验台'
OPERATION = 'exposure_coverage'
EFFECT_OPERATION = 'schedule_effect'
DEFAULT_AS_OF = '2026-09-30T23:59:59Z'

import argparse
import json

from app.httpd import create_server
from app.service import Service


def main():
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument('--source', default='data/source.json')
    parser.add_argument('--database', default='var/analyses.sqlite3')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('inspect', help='保存业务覆盖分析')
    run.add_argument('--as-of', default=DEFAULT_AS_OF)
    run.add_argument('--request-id', default='coverage-september')
    effect = commands.add_parser('effect', help='保存排班分配效果分析')
    effect.add_argument('--as-of', default=DEFAULT_AS_OF)
    effect.add_argument('--request-id', default='effect-september')
    effect.add_argument('--carryover', default='0,1,2,3',
                        help='残留影响窗口，单位为半小时时段，逗号分隔')
    server = commands.add_parser('serve', help='启动本地分析接口')
    server.add_argument('--host', default='127.0.0.1')
    server.add_argument('--port', type=int, default=8080)
    commands.add_parser('demo', help='展示当前覆盖分析')
    args = parser.parse_args()
    service = Service(args.source, args.database)
    if args.command == 'serve':
        instance = create_server(service, args.host, args.port)
        print(json.dumps({'地址': instance.server_address}, ensure_ascii=False), flush=True)
        try:
            instance.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            instance.server_close()
    elif args.command == 'effect':
        try:
            windows = [int(value) for value in args.carryover.split(',') if value.strip()]
        except ValueError as error:
            parser.error('残留影响窗口须为整数：' + str(error))
        request = {'operation': EFFECT_OPERATION, 'request_id': args.request_id,
                   'as_of': args.as_of, 'options': {'carryover_periods': windows}}
        print(json.dumps(service.run(request), ensure_ascii=False, indent=2))
    else:
        request = {'operation': OPERATION, 'request_id': getattr(args, 'request_id', 'demo-current'),
                   'as_of': getattr(args, 'as_of', DEFAULT_AS_OF)}
        print(json.dumps(service.run(request), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
