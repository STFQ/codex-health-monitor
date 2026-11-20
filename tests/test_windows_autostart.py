import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from windows_autostart import query_task, task_definition


class WindowsAutostartTest(unittest.TestCase):
    def test_redirected_task_xml_preserves_unicode_working_directory(self):
        working_dir = Path('指标监控')
        sid = 'S-1-5-21-1-2-3-1001'
        task = task_definition(sid, working_dir, ['pythonw.exe', 'run_windows.py', 'start'])
        buffer = io.BytesIO()
        task.write(buffer, encoding='utf-16', xml_declaration=True)
        # Redirecting schtasks can convert the bytes without changing the XML declaration.
        output = buffer.getvalue().decode('utf-16').encode('utf-8')
        result = subprocess.CompletedProcess(['schtasks.exe'], 0, stdout=output)

        with patch('windows_autostart.subprocess.run', return_value=result):
            registered = query_task('schtasks.exe')

        self.assertEqual(registered.findtext('{*}Actions/{*}Exec/{*}WorkingDirectory'), str(working_dir))
        self.assertEqual(registered.findtext('{*}Triggers/{*}LogonTrigger/{*}UserId'), sid)
