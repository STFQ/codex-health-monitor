import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from collect import FileCache, collect, desktop_log_files


class MonitorTest(unittest.TestCase):
    def test_desktop_log_discovery_combines_roots_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            today = datetime.now().date()
            yesterday = today - timedelta(days=1)
            direct = root / 'Codex/Logs'
            packaged = root / 'Packages/OpenAI.Codex_test/LocalCache/Local/Codex/Logs'

            def log(directory, day, name):
                path = directory / day.strftime('%Y/%m/%d') / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                return path

            primary = log(direct, today, 'codex-desktop-duplicate.log')
            log(packaged, today, primary.name)
            older = log(packaged, yesterday, 'codex-desktop-packaged.log')
            log(direct, yesterday - timedelta(days=1), 'codex-desktop-expired.log')

            self.assertEqual(desktop_log_files([direct, packaged], [today, yesterday]), {primary, older})

    def test_dns_stages_and_dashboard_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mihomo = root / 'mihomo'
            (mihomo / 'work').mkdir(parents=True)
            (mihomo / 'logs').mkdir()
            (mihomo / 'work/config.yaml').write_text(
                "dns:\n"
                "  nameserver: ['https://192.0.2.53/dns-query#Name With Spaces']\n"
                "  proxy-server-nameserver:\n"
                "    - tls://192.0.2.54:853\n",
                encoding='utf-8',
            )
            now = datetime.now().astimezone()
            start = now - timedelta(seconds=30)
            events = [
                '[TCP] mihomo --> 192.0.2.53:443 match Match using Name With Spaces[[vless]SG Test 01]',
                '[TCP] dial DIRECT (match GeoIP/cn) mihomo --> 192.0.2.54:853 error: operation was canceled',
                '[TCP] dial DIRECT (match GeoIP/cn) mihomo --> 192.0.2.54:853 error: i/o timeout',
                "[TCP] dial DIRECT (match GeoSite/cn) 198.51.100.1:60000 --> x.byted.org:443 error: dns resolve failed: couldn't find ip",
                '[TCP] mihomo --> 192.0.2.55:443 using DIRECT',
                '[TCP] 198.51.100.1:60000(codex) --> chatgpt.com:443 using codex[[vless]SG Test 01]',
                '[TCP] dial codex (match DomainSuffix/chatgpt.com) 198.51.100.1:60000(codex) --> chatgpt.com:443 error: 192.0.2.1:443 connect error: context deadline exceeded',
                '[TCP] 198.51.100.1:60000(codex) --> chatgpt.com:443 using codex[[vless]SG Test 02]',
            ]
            lines = [
                f'time="{(start + timedelta(seconds=i)).isoformat()}" msg="{message}"\n'
                for i, message in enumerate(events)
            ]
            (mihomo / 'logs' / f'core-{now.date().isoformat()}.log').write_text(
                ''.join(lines), encoding='utf-8',
            )
            args = SimpleNamespace(
                database=root / 'monitor.sqlite', days=1,
                codex_home=root / 'codex', desktop=root / 'desktop', clash=mihomo / 'logs',
            )
            with contextlib.redirect_stdout(io.StringIO()):
                collect(args, FileCache())

            with contextlib.closing(sqlite3.connect(args.database)) as db:
                self.assertEqual(
                    db.execute('SELECT event, target, route, node, cause FROM dns_events ORDER BY t').fetchall(),
                    [
                        ('upstream_routed', '192.0.2.53:443', 'Name With Spaces', '[vless]SG Test 01', None),
                        ('upstream_canceled', '192.0.2.54:853', 'DIRECT', None, 'canceled'),
                        ('upstream_error', '192.0.2.54:853', 'DIRECT', None, 'timeout'),
                        ('resolve_error', 'byted.org', 'DIRECT', None, 'dns'),
                    ],
                )
                self.assertEqual(
                    db.execute('SELECT event, node, previous_node, cause FROM clash_timeline ORDER BY t').fetchall(),
                    [
                        ('timeout', None, '[vless]SG Test 01', 'connect_timeout'),
                        ('node_observed', '[vless]SG Test 02', '[vless]SG Test 01', None),
                    ],
                )

                dashboard_path = Path(__file__).resolve().parents[1] / 'dashboard.json'
                dashboard = json.loads(dashboard_path.read_text(encoding='utf-8'))
                from_ms = int((start - timedelta(seconds=1)).timestamp() * 1000)
                to_ms = int(now.timestamp() * 1000)
                for panel in dashboard['panels']:
                    for target in panel.get('targets', []):
                        with self.subTest(panel=panel['title']):
                            sql = target['queryText'].replace('$__from', str(from_ms)).replace('$__to', str(to_ms))
                            db.execute(sql).fetchall()


if __name__ == '__main__':
    unittest.main()
