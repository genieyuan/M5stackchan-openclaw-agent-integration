"""Build a local-redirect NVS image from the device's original NVS, using only Espressif's official tools.

  python build_nvs.py build <edge-host:port>   # reads nvs_original.bin, writes nvs_local.csv
  python -m esp_idf_nvs_partition_gen generate nvs_local.csv nvs_local.bin 0x4000
  python build_nvs.py diff                     # what changed (secrets not printed)

Keeps Wi-Fi, board uuid and servo calibration; sets wifi:ota_url + websocket url/token to the local edge;
drops the vendor-cloud MQTT credentials. Needs nvs_tool.py/nvs_parser.py/nvs_logger.py/nvs_check.py from
ESP-IDF components/nvs_flash/nvs_partition_tool next to this file.
"""
import subprocess, os, csv, re, sys
PY = sys.executable
def dump(f):
    t = subprocess.run([PY, 'nvs_tool.py', '-d', 'minimal', '--color', 'never', f], capture_output=True, text=True).stdout
    v = {}
    for line in t.splitlines():
        m = re.match(r'^\s*([^:\s]+):(\S+?) = (.*)$', line)
        if m: v[(m.group(1), m.group(2))] = m.group(3).rstrip('\x00 ')
    return v
if __name__ == '__main__' and sys.argv[1:2] == ['build'] and len(sys.argv) == 3:
    EDGE = sys.argv[2]
    o = dump('nvs_original.bin'); g = lambda n, k: o[(n, k)]
    assert len(g('board', 'uuid')) == 36 and g('wifi', 'ssid') and g('wifi', 'password')
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'edge'))
    os.environ.setdefault('STACKCHAN_CONFIG', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'edge', 'config.example.json'))
    import edge as _edge; tok = _edge.device_token()
    rows = [('key','type','encoding','value'),
     ('board','namespace','',''),('uuid','data','string',g('board','uuid')),
     ('servo','namespace','',''),('zero_pos_1','data','i32',g('servo','zero_pos_1')),('zero_pos_2','data','i32',g('servo','zero_pos_2')),
     ('wifi','namespace','',''),('ssid','data','string',g('wifi','ssid')),('password','data','string',g('wifi','password')),
     ('ota_url','data','string',f'http://{EDGE}/xiaozhi/ota/'),
     ('app_config','namespace','',''),('is_configed','data','u8',g('app_config','is_configed')),
     ('websocket','namespace','',''),('url','data','string',f'ws://{EDGE}/xiaozhi/v1/'),('token','data','string',tok),
     ('audio','namespace','',''),('output_volume','data','i32','50')]
    old = os.umask(0o077)
    with open('nvs_local.csv', 'w', newline='') as f: csv.writer(f).writerows(rows)
    os.umask(old)
elif sys.argv[1:] == ['diff']:
    a, b = dump('nvs_original.bin'), dump('nvs_local.bin')
    for k in sorted(set(a) | set(b)):
        name = ':'.join(k); secret = any(s in name for s in ('password', 'ssid', 'token'))
        st = 'same' if a.get(k) == b.get(k) else 'CHANGED' if k in a and k in b else 'removed' if k in a else 'added'
        print(f"{name:26s} {st}" + ('' if secret or st in ('same', 'removed') else f"  -> {b.get(k)}"))
