#!/usr/bin/env python3
"""Deploy a SharpUpdater build to CS2 servers run by CubeCoders AMP, one at a time, and roll back on a crash. By BONE.

    deploy_amp.py <counterstrikesharp zip> [metamod tar.gz]

Environment:
  AMP_URL, AMP_USER, AMP_PASSWORD   an AMP user with FileManager and Start/Stop on the instances
  AMP_TOTP_SECRET                   that user's 2FA secret (base32), if it has 2FA
  AMP_TARGETS                       instance-id:game-port[:name], comma separated, in order; put a test or quiet server
                                    first: a crash there stops the rollout before the others are touched
  AMP_GAME_HOST                     the servers' public IP, for the "anyone playing?" check
  AMP_WAIT_EMPTY_MINUTES            how long to wait for a server to empty (default 120); still busy = skipped
  AMP_WATCH_MINUTES                 how long to watch a server after starting it (default 4)
  DEPLOY_TAG                        names the backups (default: the zip's version)

What it changes, per server: CounterStrikeSharp's engine parts (addons/counterstrikesharp/api, bin, gamedata) and
Metamod's binaries (addons/metamod/bin). Configs, plugins and Metamod's .vdf/metaplugins.ini are never touched, and
the old folders are kept as <name>.<tag>bak. A server without CounterStrikeSharp only gets Metamod.
"""
import base64, hashlib, io, json, os, re, socket, ssl, struct, sys, tarfile, time, urllib.request, zipfile

CSS = 'game/csgo/addons/counterstrikesharp'
MM = 'game/csgo/addons/metamod'
CSS_PARTS = ('api', 'bin', 'gamedata')
RUNNING, STOPPED = 20, 0


def say(*x):
    print(time.strftime('%H:%M:%S'), *x, flush=True)


def totp(secret):
    key = base64.b32decode(secret.replace(' ', '').upper() + '=' * (-len(secret.replace(' ', '')) % 8))
    digest = __import__('hmac').new(key, struct.pack('>Q', int(time.time() // 30)), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return f'{(struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}'


class Amp:
    def __init__(self, instance):
        self.prefix = f"{os.environ['AMP_URL'].rstrip('/')}/API/ADSModule/Servers/{instance}/API"
        self.sid = None
        secret = os.environ.get('AMP_TOTP_SECRET', '')
        for attempt in range(2):
            r = self.post('Core/Login', {'username': os.environ['AMP_USER'], 'password': os.environ['AMP_PASSWORD'],
                                         'token': totp(secret) if secret else '', 'rememberMe': False})
            if (r or {}).get('success') or not secret or (r or {}).get('result') != 40 or attempt:
                break
            time.sleep(31 - time.time() % 30)  # that code was just used: wait for the next one
        if not (r or {}).get('success'):
            raise SystemExit(f'AMP login to {instance} failed (result {(r or {}).get("result")})')
        self.sid = r['sessionID']

    def post(self, method, body):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if self.sid:
            headers['Authorization'] = f'Bearer {self.sid}'
        req = urllib.request.Request(f'{self.prefix}/{method}', data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=120, context=ssl.create_default_context()) as r:
            text = r.read().decode('utf-8', 'replace')
        return json.loads(text) if text.strip() else None

    def call(self, method, body=None):
        r = self.post(method, body or {})
        if isinstance(r, dict) and r.get('Title') == 'Unauthorized Access':
            raise SystemExit(f'AMP refused {method}: {r.get("Message", "")[:200]}')
        return r

    def state(self):
        return (self.call('Core/GetStatus') or {}).get('State')

    def ls(self, d):
        r = self.call('FileManagerPlugin/GetDirectoryListing', {'Dir': d})
        r = r.get('result', r) if isinstance(r, dict) else r
        return {x['Filename']: x for x in r} if isinstance(r, list) else {}

    def ok(self, method, body):
        r = self.call(method, body)
        r = r.get('result', r) if isinstance(r, dict) and 'Status' not in r else r
        return bool(r is True or (isinstance(r, dict) and r.get('Status')))

    def write(self, path, data, chunk=1_000_000):
        for off in range(0, len(data), chunk):
            if not self.ok('FileManagerPlugin/WriteFileChunk', {'Filename': path, 'Offset': off,
                                                                'Data': base64.b64encode(data[off:off + chunk]).decode(),
                                                                'FinalChunk': off + chunk >= len(data)}):
                raise SystemExit(f'upload of {path} failed at {off}')

    def md5(self, path):
        return ((self.call('FileManagerPlugin/CalculateFileMD5Sum', {'FilePath': path}) or {}).get('Result') or '').lower()

    def rename_dir(self, path, new_name):
        return self.ok('FileManagerPlugin/RenameDirectory', {'oldDirectory': path, 'NewDirectoryName': new_name})

    def console(self):
        return [e.get('Contents') or '' for e in (self.call('Core/GetUpdates') or {}).get('ConsoleEntries') or []]

    def stop(self):
        self.call('Core/Stop')
        for _ in range(60):
            if self.state() == STOPPED:
                return True
            time.sleep(2)
        return False


def humans(host, port):
    """Human players from the Steam query, None if the server doesn't answer (down, loading or crash-looping)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        q = b'\xff\xff\xff\xffTSource Engine Query\x00'
        s.sendto(q, (host, port))
        d, _ = s.recvfrom(4096)
        if d[4] == 0x41:
            s.sendto(q + d[5:9], (host, port))
            d, _ = s.recvfrom(4096)
        p = 6
        for _ in range(4):
            p = d.index(b'\x00', p) + 1
        return d[p + 2] - d[p + 4]
    except (OSError, ValueError, IndexError):
        return None


def build_payload(css_zip, mm_tar):
    """One zip laid out from game/csgo, with only the parts this deploys, and the md5 of the files that prove it landed."""
    buf, check = io.BytesIO(), {}
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as out:
        z = zipfile.ZipFile(css_zip)
        for n in z.namelist():
            parts = n.split('/')
            if len(parts) > 3 and parts[:2] == ['addons', 'counterstrikesharp'] and parts[2] in CSS_PARTS and not n.endswith('/'):
                out.writestr(n, z.read(n))
        so = 'addons/counterstrikesharp/bin/linuxsteamrt64/counterstrikesharp.so'
        check['css'] = (f'game/csgo/{so}', hashlib.md5(z.read(so)).hexdigest())
        if mm_tar:
            with tarfile.open(mm_tar) as t:
                for m in t.getmembers():
                    if m.isfile() and m.name.lstrip('./').startswith('addons/metamod/bin/'):
                        out.writestr(m.name.lstrip('./'), t.extractfile(m).read())
                        if m.name.endswith('metamod.2.cs2.so'):
                            check['mm'] = (f"game/csgo/{m.name.lstrip('./')}", hashlib.md5(t.extractfile(m).read()).hexdigest())
    return buf.getvalue(), check


def deploy(target, payload, check, tag, host, wait_empty, watch):
    iid, port, name = target
    a = Amp(iid)
    say(f'[{name}] state {a.state()}')

    deadline = time.time() + wait_empty * 60
    while (h := humans(host, port)) and time.time() < deadline:
        say(f'[{name}] {h} playing; waiting for the server to empty')
        time.sleep(60)
    if h:
        say(f'[{name}] still {h} playing after {wait_empty} min: skipped')
        return 'skipped'

    has_css = 'counterstrikesharp' in a.ls('game/csgo/addons')
    # Only expect CounterStrikeSharp to start where Metamod loads it (its .vdf can be switched off)
    css_enabled = has_css and 'counterstrikesharp.vdf' in a.ls(MM)
    moves = ([(CSS, p) for p in CSS_PARTS] if has_css else []) + ([(MM, 'bin')] if 'mm' in check else [])
    if not moves:
        say(f'[{name}] has neither CounterStrikeSharp nor a Metamod build to replace: skipped')
        return 'skipped'
    for folder, part in moves:
        if f'{part}.{tag}bak' in a.ls(folder):
            raise SystemExit(f'[{name}] {folder}/{part}.{tag}bak already exists; this build was deployed before. Stopping.')

    if not a.stop():
        raise SystemExit(f'[{name}] did not stop within 2 minutes; nothing changed')
    say(f'[{name}] stopped')

    def restore():
        for folder, part in moves:
            have = a.ls(folder)
            if f'{part}.{tag}bak' in have:
                if part in have:
                    a.call('FileManagerPlugin/TrashDirectory', {'DirectoryName': f'{folder}/{part}'})
                a.rename_dir(f'{folder}/{part}.{tag}bak', part)

    for folder, part in moves:
        if not a.rename_dir(f'{folder}/{part}', f'{part}.{tag}bak'):
            restore()
            a.call('Core/Start')
            raise SystemExit(f'[{name}] could not move {folder}/{part} aside; put back and started')
    a.write('game/csgo/sharpupdater-deploy.zip', payload)
    a.call('FileManagerPlugin/ExtractArchive', {'ArchivePath': 'game/csgo/sharpupdater-deploy.zip', 'DestinationPath': 'game/csgo'})
    wanted = [v for k, v in check.items() if k == 'mm' or has_css]
    for _ in range(45):
        time.sleep(2)
        if all(a.md5(path) == digest for path, digest in wanted):
            break
    else:
        restore()
        a.call('Core/Start')
        raise SystemExit(f'[{name}] the new files did not arrive intact; old version put back and started')
    a.call('FileManagerPlugin/TrashFile', {'Filename': 'game/csgo/sharpupdater-deploy.zip'})
    say(f'[{name}] new files in place ({", ".join(p.rsplit("/", 1)[-1] for p, _ in wanted)} md5 verified)')

    a.console()  # drop the old console backlog
    a.call('Core/Start')
    # AMP may first update the game on start (states 70/75) and takes a while to get to Ready (20). The watch window
    # starts at Ready; a crash (segfault in the console, Failed, or stopped) at any point rolls back.
    started, running_since, crashed, css_up, answered = time.time(), None, None, False, False
    while True:
        time.sleep(5)
        for line in a.console():
            if 'CounterStrikeSharp is starting up' in line:
                css_up = True
            if re.search(r'segmentation fault|signal 11|core dumped|exited with code 139', line, re.I):
                crashed = line.strip()[:200]
        st = a.state()
        if st == RUNNING and running_since is None:
            running_since = time.time()
        if st in (STOPPED, 100) and time.time() - started > 20:  # 100 = Failed
            crashed = crashed or f'AMP state {st}'
        if running_since is not None and st != RUNNING:
            crashed = crashed or f'left Running (AMP state {st}): it crashed and AMP is restarting it'
        if crashed:
            break
        if running_since is None:
            if time.time() - started > 30 * 60:
                crashed = 'never reached Running within 30 minutes'
                break
            continue
        answered = answered or humans(host, port) is not None
        if time.time() - running_since > watch * 60:
            break
    if not crashed and not answered:
        crashed = f'not answering Steam queries {watch} min after starting'
    if not crashed and css_enabled and not css_up:
        crashed = 'CounterStrikeSharp never reported starting up'
    if crashed:
        say(f'[{name}] FAILED: {crashed}. Rolling back.')
        a.stop()
        restore()
        a.call('Core/Start')
        raise SystemExit(f'[{name}] rolled back to the previous build and restarted; the rollout stopped here')
    say(f'[{name}] up and answering; CounterStrikeSharp '
        f'{"started" if css_up else "switched off (no counterstrikesharp.vdf)" if has_css else "not installed"}')
    return 'deployed'


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    css_zip, mm_tar = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None)
    targets = []
    for t in os.environ['AMP_TARGETS'].split(','):
        bits = t.strip().split(':')
        if len(bits) >= 2:
            targets.append((bits[0], int(bits[1]), bits[2] if len(bits) > 2 else bits[0][:8]))
    tag = re.sub(r'[^A-Za-z0-9]+', '-', os.environ.get('DEPLOY_TAG') or re.sub(r'^.*linux-|\.zip$', '', os.path.basename(css_zip)))[:40]
    payload, check = build_payload(css_zip, mm_tar)
    say(f'payload {len(payload)} bytes, backups tagged {tag}, {len(targets)} server(s)')
    results = {}
    for target in targets:
        results[target[2]] = deploy(target, payload, check, tag, os.environ['AMP_GAME_HOST'],
                                    float(os.environ.get('AMP_WAIT_EMPTY_MINUTES', 120)),
                                    float(os.environ.get('AMP_WATCH_MINUTES', 4)))
    say('done:', ', '.join(f'{k} {v}' for k, v in results.items()))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write('### AMP deploy\n' + ''.join(f'- {k}: {v}\n' for k, v in results.items()))


if __name__ == '__main__':
    main()
