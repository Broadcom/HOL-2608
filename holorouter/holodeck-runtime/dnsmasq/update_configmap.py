from __future__ import print_function
import sys
import time
from datetime import datetime, timezone
import kubernetes.client
import kubernetes.config
from kubernetes.client.rest import ApiException

kubernetes.config.load_kube_config()

api_instance   = kubernetes.client.CoreV1Api()
api_apps       = kubernetes.client.AppsV1Api()

name            = 'dnsmasq'
deployment_name = 'dnsmasq-deployment'
namespace       = 'default'
pretty          = 'true'
op_type         = sys.argv[1]

# --- 1. Read and mutate the ConfigMap ---
try:
    cm = api_instance.read_namespaced_config_map(name, namespace, pretty=pretty)
except ApiException as e:
    sys.exit(f'ERROR reading ConfigMap: {e}')

if op_type == 'create':
    dns_entry = sys.argv[2]
    if dns_entry in cm.data['hosts']:
        sys.exit('The DNS entry already exists in the DNS server')
    cm.data['hosts'] += '\n' + dns_entry

elif op_type == 'update':
    search_entry  = sys.argv[2]
    replace_entry = sys.argv[3]
    if search_entry not in cm.data['hosts']:
        sys.exit('The DNS entry does not exist in the DNS server')
    cm.data['hosts'] = cm.data['hosts'].replace(search_entry, replace_entry)

else:
    sys.exit(f'Unknown operation: {op_type}. Use create or update.')

# --- 2. Patch the ConfigMap ---
try:
    api_instance.patch_namespaced_config_map(name, namespace, cm, pretty=pretty)
    print(f'ConfigMap updated ({op_type})')
except ApiException as e:
    sys.exit(f'ERROR patching ConfigMap: {e}')

# --- 3. Trigger a rollout restart of the Deployment.
#    With strategy.type=Recreate this terminates the old pod first,
#    then starts a new pod that mounts the updated ConfigMap.
#    This replaces the previous delete+recreate approach which caused
#    a FailedScheduling race on hostPort 53 (old pod sandbox held the
#    port while the new pod was already being scheduled).
try:
    restart_patch = {
        'spec': {
            'template': {
                'metadata': {
                    'annotations': {
                        'kubectl.kubernetes.io/restartedAt': datetime.now(timezone.utc).isoformat()
                    }
                }
            }
        }
    }
    api_apps.patch_namespaced_deployment(deployment_name, namespace, restart_patch)
    print('Rollout restart triggered (strategy: Recreate)')
    print('DNS Record successfully updated')
except ApiException as e:
    sys.exit(f'ERROR triggering rollout restart: {e}')
