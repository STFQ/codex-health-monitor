"""Register the current user's Codex Health logon task."""

import csv
import os
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree

TASK_NAME = 'Codex Health Monitor'
TASK_NAMESPACE = 'http://schemas.microsoft.com/windows/2004/02/mit/task'


def task_definition(sid, working_dir, command):
    task = ElementTree.Element('Task', {'version': '1.2', 'xmlns': TASK_NAMESPACE})
    registration = ElementTree.SubElement(task, 'RegistrationInfo')
    ElementTree.SubElement(registration, 'Description').text = 'Start the local Codex Health dashboard at sign-in.'

    triggers = ElementTree.SubElement(task, 'Triggers')
    trigger = ElementTree.SubElement(triggers, 'LogonTrigger')
    ElementTree.SubElement(trigger, 'UserId').text = sid

    principals = ElementTree.SubElement(task, 'Principals')
    principal = ElementTree.SubElement(principals, 'Principal', {'id': 'CurrentUser'})
    ElementTree.SubElement(principal, 'UserId').text = sid
    ElementTree.SubElement(principal, 'LogonType').text = 'InteractiveToken'
    ElementTree.SubElement(principal, 'RunLevel').text = 'LeastPrivilege'

    settings = ElementTree.SubElement(task, 'Settings')
    ElementTree.SubElement(settings, 'MultipleInstancesPolicy').text = 'IgnoreNew'
    ElementTree.SubElement(settings, 'DisallowStartIfOnBatteries').text = 'false'
    ElementTree.SubElement(settings, 'StopIfGoingOnBatteries').text = 'false'

    actions = ElementTree.SubElement(task, 'Actions', {'Context': 'CurrentUser'})
    action = ElementTree.SubElement(actions, 'Exec')
    ElementTree.SubElement(action, 'Command').text = command[0]
    ElementTree.SubElement(action, 'Arguments').text = subprocess.list2cmdline(command[1:])
    ElementTree.SubElement(action, 'WorkingDirectory').text = str(working_dir)
    return ElementTree.ElementTree(task)


def query_task(schtasks):
    result = subprocess.run([schtasks, '/query', '/tn', TASK_NAME, '/xml', '/hresult'], capture_output=True)
    # HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND) means this task has not been created.
    if result.returncode in (0x80070002, -2147024894):
        return None
    result.check_returncode()
    xml = result.stdout
    if b'\0' not in xml[:32]:
        # Redirected schtasks output may use a console code page despite its UTF-16 declaration.
        try:
            xml = xml.decode('utf-8-sig')
        except UnicodeDecodeError:
            xml = xml.decode('oem', errors='replace')
    return ElementTree.fromstring(xml)


def autostart(operation, working_dir, command=None):
    system = Path(os.environ['SystemRoot']) / 'System32'
    schtasks = str(system / 'schtasks.exe')
    if operation == 'enable-autostart':
        account = subprocess.check_output(
            [str(system / 'whoami.exe'), '/user', '/fo', 'csv', '/nh'], text=True, errors='replace',
        )
        sid = next(csv.reader(account.splitlines()))[1]
        with tempfile.TemporaryDirectory() as temporary:
            xml = Path(temporary) / 'task.xml'
            task_definition(sid, working_dir, command).write(xml, encoding='utf-16', xml_declaration=True)
            subprocess.run([schtasks, '/create', '/tn', TASK_NAME, '/xml', str(xml), '/f'], check=True)
        print('Autostart enabled for the current Windows user.')
    elif operation == 'disable-autostart':
        subprocess.run([schtasks, '/change', '/tn', TASK_NAME, '/disable'], check=True)
        print('Autostart disabled.')
    else:
        task = query_task(schtasks)
        if task is None:
            print('Autostart has not been registered.')
            return
        enabled = task.findtext('{*}Settings/{*}Enabled', 'true').lower() == 'true'
        print(f'Autostart: {"enabled" if enabled else "disabled"} ({TASK_NAME})')
