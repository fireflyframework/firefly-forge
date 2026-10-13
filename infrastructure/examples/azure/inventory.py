import json,subprocess
from pathlib import Path
host=json.loads(subprocess.check_output(['tofu','-chdir=.forge-deployment','output','-json','host']))
if not isinstance(host,str) or not host or any(c in host for c in '\n\r /'): raise ValueError('Invalid VM host')
inventory={'all':{'hosts':{'application':{'ansible_host':host,'ansible_user':'ubuntu'}}}}
Path('.forge-deployment/inventory.json').write_text(json.dumps(inventory))
