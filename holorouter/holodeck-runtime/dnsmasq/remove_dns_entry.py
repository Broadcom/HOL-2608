from __future__ import print_function
import sys
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

# --- 1. Read and mutate the ConfigMap ---
try:
    cm = api_instance.read_namespaced_config_map(name, namespace, pretty=pretty)
except ApiException as e:
    sys.exit(f'ERROR reading ConfigMap: {e}')

dns_entry = sys.argv[1]
if dns_entry not in cm.data['hosts']:
    sys.exit('The DNS entry does not exist in the DNS server')

cm.data['hosts'] = cm.data['hosts'].replace(dns_entry, '').strip()

# --- 2. Patch the ConfigMap ---
try:
    api_instance.patch_namespaced_config_map(name, namespace, cm, pretty=pretty)
    print('ConfigMap updated (remove)')
except ApiException as e:
    sys.exit(f'ERROR patching ConfigMap: {e}')

# --- 3. Trigger rollout restart (Recreate strategy - no hostPort race condition) ---
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
    print('DNS Record successfully deleted')
except ApiException as e:
    sys.exit(f'ERROR triggering rollout restart: {e}')
