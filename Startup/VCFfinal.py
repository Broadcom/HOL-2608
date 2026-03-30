# VCFfinal.py version 1.4 2026-03-25
import datetime
import os
import sys
import ssl
import json
import time
import urllib.request
from pyVim import connect
from pyVmomi import vim
import logging
import requests
import urllib3
import lsfunctions as lsf

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SDDC = 'sddcmanager-a.site-a.vcf.lab'
STALE_CREDENTIAL_PATTERNS = [
    'svc-sddcmanager-a-nsx-mgmt-',
    'svc-sddcmanager-a-nsx-wld01-',
]


def cleanup_stale_sddc_credentials():
    """
    Remove stale NSX service account credentials from SDDC Manager.
    These accounts no longer exist but cause error banners in vCenter.
    Deletion must be done via direct PostgreSQL access since the
    SDDC Manager REST API does not support DELETE on credentials.
    Uses a self-contained Python script SCP'd to SDDC Manager to avoid
    shell quoting issues with lsf.ssh().
    Stale accounts were
    svc-sddcmanager-a-nsx-mgmt--5825
    svc-sddcmanager-a-nsx-wld01-1894
    """
    pw = lsf.password

    try:
        resp = requests.post(
            f'https://{SDDC}/v1/tokens',
            json={'username': 'admin@local', 'password': pw},
            verify=False, timeout=30
        )
        if resp.status_code != 200:
            lsf.write_output(f'SDDC credential cleanup: cannot get token (HTTP {resp.status_code}), skipping')
            return
        token = resp.json().get('accessToken', '')
    except Exception as e:
        lsf.write_output(f'SDDC credential cleanup: token request failed ({e}), skipping')
        return

    try:
        resp = requests.get(
            f'https://{SDDC}/v1/credentials',
            headers={'Authorization': f'Bearer {token}'},
            verify=False, timeout=30
        )
        elements = resp.json().get('elements', [])
    except Exception as e:
        lsf.write_output(f'SDDC credential cleanup: credentials query failed ({e}), skipping')
        return

    stale = [el for el in elements
             if any(el.get('username', '').startswith(pat) for pat in STALE_CREDENTIAL_PATTERNS)]

    if not stale:
        lsf.write_output('SDDC credential cleanup: no stale NSX service accounts found')
        return

    stale_names = [el['username'] for el in stale]
    stale_ids = [el['id'] for el in stale]
    lsf.write_output(f'SDDC credential cleanup: found {len(stale)} stale account(s): {", ".join(stale_names)}')

    local_script = '/tmp/sddc_cred_cleanup.py'
    remote_script = '/tmp/sddc_cred_cleanup.py'

    script_lines = [
        'import pty, os, time, sys, json',
        '',
        'PW = ' + repr(pw),
        'IDS = ' + repr(stale_ids),
        '',
        'def su_root_cmd(commands):',
        '    m, s = pty.openpty()',
        '    pid = os.fork()',
        '    if pid == 0:',
        '        os.setsid()',
        '        os.dup2(s, 0); os.dup2(s, 1); os.dup2(s, 2); os.close(m)',
        '        os.execvp("su", ["su", "-", "root"])',
        '    time.sleep(1)',
        '    os.write(m, (PW + "\\n").encode())',
        '    time.sleep(1)',
        '    for cmd in commands:',
        '        os.write(m, (cmd + "\\n").encode())',
        '        time.sleep(2)',
        '    os.write(m, b"exit\\n")',
        '    time.sleep(0.5)',
        '    data = os.read(m, 16384).decode(errors="ignore")',
        '    os.waitpid(pid, 0)',
        '    return data',
        '',
        'output = su_root_cmd(["cat /root/.pgpass"])',
        'pgpass = ""',
        'for line in output.splitlines():',
        '    if line.startswith("localhost") and "postgres:" in line:',
        '        pgpass = line.split(":")[-1]',
        '        break',
        '',
        'if not pgpass:',
        '    print("ERROR: could not extract pgpass")',
        '    sys.exit(1)',
        '',
        "id_list = ','.join(\"'\" + cid + \"'\" for cid in IDS)",
        'sql = "DELETE FROM credentialhistory WHERE credential_id IN (" + id_list + "); '
        'DELETE FROM credential WHERE id IN (" + id_list + ");"',
        "psql_cmd = \"PGPASSWORD='\" + pgpass + \"' /usr/pgsql/15/bin/psql -h 127.0.0.1 -U postgres -d platform -c \\\"\" + sql + \"\\\"\"",
        '',
        'output = su_root_cmd([psql_cmd])',
        'print("SQL_DONE")',
        'for line in output.splitlines():',
        '    if "DELETE" in line:',
        '        print(line)',
        '',
        'su_root_cmd(["systemctl restart operationsmanager commonsvcs domainmanager"])',
        'print("RESTART_DONE")',
    ]

    with open(local_script, 'w') as f:
        f.write('\n'.join(script_lines) + '\n')

    lsf.scp(local_script, f'vcf@{SDDC}:{remote_script}', pw)
    result = lsf.ssh(f'python3 {remote_script}', f'vcf@{SDDC}', pw)
    lsf.ssh(f'rm -f {remote_script}', f'vcf@{SDDC}', pw)
    try:
        os.remove(local_script)
    except OSError:
        pass

    lsf.write_output(f'SDDC credential cleanup: result: {result.stdout.strip()}')


def verify_nic_connected (vm_obj, simple):
    """
    Loop through the NICs and verify connection
    :param vm: the VM to check
    :param simple: true just connect do not disconnect then reconnect
    """
    nics = lsf.get_network_adapter(vm_obj)
    for nic in nics:
        if simple:
            lsf.write_output(f'Connecting {nic.deviceInfo.label} on {vm.name} .')
            lsf.set_network_adapter_connection(vm, nic, True)
            lsf.labstartup_sleep(lsf.sleep_seconds)
        elif nic.connectable.connected == True:
            lsf.write_output(f'{vm.name} {nic.deviceInfo.label} is connected.')
        else:
            lsf.write_output(f'{vm.name} {nic.deviceInfo.label} is NOT connected.')
            lsf.set_network_adapter_connection(vm, nic, False)
            lsf.labstartup_sleep(lsf.sleep_seconds)
            lsf.write_output(f'Connecting {nic.deviceInfo.label} on {vm.name} .')
            lsf.set_network_adapter_connection(vm, nic, True)

# read the /hol/config.ini
lsf.init(router=False)

# verify a VCFfinal section exists
if lsf.config.has_section('VCFFINAL') == False:
    lsf.write_output('Skipping VCF final startup')
    exit(0)

color = 'red'
if len(sys.argv) > 1:
    lsf.start_time = datetime.datetime.now() - datetime.timedelta(seconds=int(sys.argv[1]))
    if sys.argv[2] == "True":
        lsf.labcheck = True
        color = 'green'
        lsf.write_output(f'{sys.argv[0]}: labcheck is {lsf.labcheck}')   
    else:
        lsf.labcheck = False
 
lsf.write_output(f'Running {sys.argv[0]}')
lsf.write_vpodprogress('Tanzu Start', 'GOOD-8', color=color)

### Start SupervisorControlPlaneVMs
vcfmgmtcluster = []
if 'vcfmgmtcluster' in lsf.config['VCF'].keys():
    vcfmgmtcluster = lsf.config.get('VCF', 'vcfmgmtcluster').split('\n')
    lsf.write_vpodprogress('VCF Hosts Connect', 'GOOD-3', color=color)
    lsf.connect_vcenters(vcfmgmtcluster)

lsf.write_vpodprogress('Tanzu Control Plane', 'GOOD-8', color=color)
supvms = lsf.get_vm_match('Supervisor*')
for vm in supvms:
   lsf.write_output(f'{vm.name} is {vm.runtime.powerState}')
   try:
        if vm.runtime.powerState != "poweredOn":
            lsf.start_nested([f'{vm.name}:{vm.runtime.host.name}'])
   except Exception as e:
        lsf.write_output(f'exception: {e}')

### Reconnect SupervisorControlPlaneVM NICs
for vm in supvms:
    verify_nic_connected (vm, False) # if not connected, disconnet then reconnect

## Restart Supervisor Webhooks to make sure certificate is valid/renewed
# if supvms list is not empty, then restart the webhooks
if supvms:
    lsf.write_output(f'Restarting Supervisor Webhooks')
    lsf.run_command("/home/holuser/hol/Tools/restart_k8s_webhooks.sh")
                
# Wizardry to deploy Tanzu

tanzucreate = []
if 'tanzucreate' in lsf.config['VCFFINAL'].keys():
    lsf.write_vpodprogress('Deploy Tanzu (25 Minutes)', 'GOOD-8', color=color)
    lsf.write_output('Deploy Tanzu (25 Minutes)')
    tanzucreate = lsf.config.get('VCFFINAL', 'tanzucreate').split('\n')
    lsf.write_vpodprogress('Waiting for Tanzu img to populate', 'GOOD-8', color=color)
    lsf.write_output('Waiting for Tanzu Images (10 minutes)...')
    # DEBUG skip this for dev testing - is there a test we can do?
    lsf.labstartup_sleep(600)

    # centos machine is 10.0.0.3 /root/TanzuCreate script. recommend DNS entry
    (tchost, tcaccount, tcscript) = tanzucreate[0].split(':')
    lsf.write_output(f'Running {tcscript} as {tcaccount}@{tchost} with password lsf.password')
    # DEBUG comment out
    result = lsf.ssh(tcscript, f'{tcaccount}@{tchost}', lsf.password, logfile=lsf.logfile)
    lsf.write_output(result.stdout)

######Start Aria Automation VMs
# Could we start this during the 10 minutes we're waiting for Tanzu?
vravms = []
if 'vravms' in lsf.config['VCFFINAL'].keys():
    vcenters = []
    if 'vCenters' in lsf.config['RESOURCES'].keys():
        vcenters = lsf.config.get('RESOURCES', 'vCenters').split('\n')

    if vcenters:
        lsf.write_vpodprogress('Connecting vCenters', 'GOOD-3', color=color)
        lsf.connect_vcenters(vcenters)
    vravms = lsf.config.get('VCFFINAL', 'vravms').split('\n')
    lsf.write_output('Starting Workspace Access...')
    lsf.write_vpodprogress('Starting Workspace Access', 'GOOD-8', color=color)
    # before starting verify NICs are set to start connected
    for vravm in vravms:
        (vmname, server) = vravm.split(':')
        try:
            vms = lsf.get_vm_match(vmname)
            for vm in vms:
                verify_nic_connected (vm, True) # just make sure connected at start
        except Exception as e:
            lsf.write_output(f'{e}')
    lsf.start_nested(vravms)
    # verify that the wsa L2 VM is actually starting
    # after starting verify NIC is actually connected
    for vravm in vravms:
        (vmname, server) = vravm.split(':')
        vms = lsf.get_vm_match(vmname)
        for vm in vms:
            while vm.runtime.powerState != 'poweredOn':
                vm.PowerOnVM_Task()
                lsf.labstartup_sleep(lsf.sleep_seconds)
            while vm.summary.guest.toolsRunningStatus != 'guestToolsRunning':
                lsf.write_output(f'Waiting for Tools in {vmname}...')
                lsf.labstartup_sleep(lsf.sleep_seconds)
                verify_nic_connected (vm, False) # if not connected, disconnect and reconnect
    
##### Final URL Checking
vraurls = []
if 'vraurls' in lsf.config['VCFFINAL'].keys():
    vraurls = lsf.config.get('VCFFINAL', 'vraurls').split('\n')
    lsf.write_vpodprogress('Aria Automation URL Checks', 'GOOD-8', color=color)
    lsf.write_output('Aria Automation URL Checks...')
    # Check VCF Automation ssh for password expiration and fix if expired
    lsf.write_output('Fixing expired automation pw if necessary...')
    lsf.run_command("/home/holuser/hol/Tools/vcfapwcheck.sh")
    # Run the watchvcfa script to make sure the seaweedfs-master-0 pod is not stale
    lsf.run_command("/home/holuser/hol/Tools/watchvcfa.sh")

    for entry in vraurls:
        url = entry.split(',')
        lsf.write_output(f'Testing {url[0]} for pattern {url[1]}')
        #  not_ready: optional pattern if present means not ready verbose: display the html
        #  lsf.test_url(url[0], pattern=url[1], not_ready='not yet', verbose=True)
        ctr = 0
        while not lsf.test_url(url[0], pattern=url[1], timeout=2, verbose=False):
            ctr += 1
            # If the URL is still unreachable after 30m, even with remediation attempt, then fail the pod
            if ctr == 30:
                lsf.labfail('fail: Automation URLS not accessible after 30m, should be reached in under 8m')
                exit(1)
                # Try to prevent excessive logging while waiting for VLP to stop vApp
                lsf.labstartup_sleep(120)
            # Wait for 1m before retrying
            lsf.write_output(f'Sleeping and will try again... {ctr} / 30')
            lsf.labstartup_sleep(60)             

### Clean up stale SDDC Manager credentials that cause vCenter error banners
cleanup_stale_sddc_credentials()

##### Fix opsnet-01a service account for VCF Operations integration
def fix_opsnet_service_account():
    """
    Recreates the svc_ops_ops-ni_3e8 service account on opsnet-01a with correct
    userType=LOCAL and serviceAccount=true in FoundationDB, then updates the
    VCF Operations adapter credential to use the lab password.
    
    The opsnet appliance stores users in FoundationDB, and the built-in UserTool
    creates accounts with userType=DEFAULT which cannot authenticate. This uses
    the AuthStore Java API directly to create a proper LOCAL service account.
    """
    ctx = ssl._create_unverified_context()
    password = lsf.password
    svc_account = 'svc_ops_ops-ni_3e8'
    customer_id = '18482'

    lsf.write_output('Fixing opsnet-01a service account...')

    si = None
    for s in lsf.sis:
        try:
            if s.content.about.instanceUuid:
                si = s
                break
        except Exception:
            continue

    if si is None:
        lsf.write_output('No vCenter connection available for opsnet fix')
        return False

    content = si.RetrieveContent()
    opsnet_vm = None
    for vm in content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    ).view:
        if vm.name == 'opsnet-01a':
            opsnet_vm = vm
            break

    if opsnet_vm is None:
        lsf.write_output('opsnet-01a VM not found')
        return False

    if opsnet_vm.runtime.powerState != 'poweredOn':
        lsf.write_output('opsnet-01a is not powered on, skipping')
        return False

    creds = vim.vm.guest.NamePasswordAuthentication(
        username="root", password=password
    )
    pm = content.guestOperationsManager.processManager
    fm = content.guestOperationsManager.fileManager

    java_src = f'''
import com.vnera.storage.config.AuthStore;
import com.vnera.storage.config.AuthStore.UserData;
import com.vnera.storage.config.AuthStore.IndentityProvider;
import com.vnera.storage.config.AuthStore.UserRoleData;
import com.vnera.storage.config.AuthStore.AuthFailure;
import com.vnera.storage.config.ConfigStoreFactory;
import com.vnera.storage.config.ConfigStore;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Level;
import org.apache.logging.log4j.core.config.Configurator;

public class FixOpsnetSvc {{
    public static void main(String[] args) throws Exception {{
        Configurator.setLevel(LogManager.getRootLogger().getName(), Level.OFF);
        String userId = args[0];
        String hashedPw = args[1];
        int cid = Integer.parseInt(args[2]);

        ConfigStore cs = ConfigStoreFactory.getInstance()
            .getOrCreateStore(ConfigStoreFactory.StoreType.PSQL);
        AuthStore as = cs.getAuthStore();

        UserData ex = as.getUser(userId);
        if (ex != null && ex.userEmail != null) {{
            as.deleteUser(cid, userId);
            System.out.println("DELETED_EXISTING");
        }}

        UserData ud = new UserData(cid, userId, userId,
            false, true, "admin@local",
            System.currentTimeMillis(), true, false,
            IndentityProvider.LOCAL, null);
        ud.setServiceAccount(true);
        as.createUser(ud, hashedPw);

        try {{
            as.addUserRole(new UserRoleData(cid, userId, "ADMIN", "admin@local"));
        }} catch (Exception e) {{
            // Role may already exist
        }}
        as.updateAuthFailure(userId, new AuthFailure(0, 0));

        UserData v = as.getUser(userId);
        if (v != null) {{
            System.out.println("TYPE=" + v.getUserType());
            System.out.println("SVC=" + v.isServiceAccount());
            System.out.println("SUCCESS");
        }}
        cs.shutdown();
        System.exit(0);
    }}
}}
'''

    script = f'''#!/bin/bash
cd /home/ubuntu/build-target
JAVA=/usr/lib/jvm/openjdk-java17-amd64/bin/java
JAVAC=/usr/lib/jvm/openjdk-java17-amd64/bin/javac
CP="/home/ubuntu/build-target/common-utils/tools-0.001-SNAPSHOT.jar"
PW='{password}'

# Hash the password
echo "$PW" > /tmp/.r
HASHED=$($JAVA -jar /home/ubuntu/build-target/cli/shiro-tools-hasher-1.12.0-cli.jar \\
    -a SHA-256 -f shiro1 -i 500000 -gs -r /tmp/.r 2>/dev/null)
rm -f /tmp/.r

cat > /tmp/FixOpsnetSvc.java << 'JAVAEOF'
{java_src}
JAVAEOF

$JAVAC -cp "$CP" /tmp/FixOpsnetSvc.java -d /tmp/ 2>&1 | grep -v "^warning"
$JAVA -cp "/tmp:$CP" FixOpsnetSvc "{svc_account}" "$HASHED" "{customer_id}" 2>&1

# Also reset auth failures for admin@local
cat > /tmp/ResetLockout.java << 'JAVAEOF2'
import com.vnera.storage.config.AuthStore;
import com.vnera.storage.config.AuthStore.AuthFailure;
import com.vnera.storage.config.ConfigStoreFactory;
import com.vnera.storage.config.ConfigStore;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Level;
import org.apache.logging.log4j.core.config.Configurator;
public class ResetLockout {{
    public static void main(String[] args) throws Exception {{
        Configurator.setLevel(LogManager.getRootLogger().getName(), Level.OFF);
        ConfigStore cs = ConfigStoreFactory.getInstance()
            .getOrCreateStore(ConfigStoreFactory.StoreType.PSQL);
        AuthStore as = cs.getAuthStore();
        for (String u : args) {{
            as.updateAuthFailure(u, new AuthFailure(0, 0));
            System.out.println("RESET_LOCKOUT=" + u);
        }}
        cs.shutdown();
        System.exit(0);
    }}
}}
JAVAEOF2
$JAVAC -cp "$CP" /tmp/ResetLockout.java -d /tmp/ 2>&1 | grep -v "^warning"
$JAVA -cp "/tmp:$CP" ResetLockout "{svc_account}" "admin@local" 2>&1
'''

    try:
        script_bytes = script.encode('utf-8')
        attr = vim.vm.guest.FileManager.FileAttributes()
        url = fm.InitiateFileTransferToGuest(
            opsnet_vm, creds, '/tmp/fix_svc.sh', attr, len(script_bytes), True
        )
        req = urllib.request.Request(url, data=script_bytes, method='PUT')
        req.add_header('Content-Type', 'application/octet-stream')
        urllib.request.urlopen(req, context=ctx)

        spec = vim.vm.guest.ProcessManager.ProgramSpec(
            programPath="/bin/bash",
            arguments="-c 'chmod +x /tmp/fix_svc.sh && /tmp/fix_svc.sh > /tmp/fix_svc_output.txt 2>&1'"
        )
        pm.StartProgramInGuest(opsnet_vm, creds, spec)
        time.sleep(40)

        info = fm.InitiateFileTransferFromGuest(
            opsnet_vm, creds, '/tmp/fix_svc_output.txt'
        )
        resp = urllib.request.urlopen(info.url, context=ctx)
        output = resp.read().decode('utf-8', errors='replace')

        if 'SUCCESS' in output:
            lsf.write_output('opsnet-01a service account created successfully')
        else:
            lsf.write_output(f'opsnet-01a service account fix output: {output[:500]}')
            return False

    except Exception as e:
        lsf.write_output(f'opsnet-01a service account fix failed: {e}')
        return False

    # Update VCF Operations adapter credential
    try:
        lsf.write_output('Updating VCF Operations adapter credential...')
        ops_host = 'ops-a.site-a.vcf.lab'

        token_body = json.dumps({
            "username": "admin", "authSource": "local", "password": password
        }).encode()
        req = urllib.request.Request(
            f'https://{ops_host}/suite-api/api/auth/token/acquire',
            data=token_body, method='POST'
        )
        req.add_header('Content-Type', 'application/json')
        resp = urllib.request.urlopen(req, context=ctx)
        ops_token = json.loads(resp.read()).get('token', '')

        if not ops_token:
            lsf.write_output('Failed to get VCF Ops token')
            return False

        ops_headers = {
            'Authorization': f'OpsToken {ops_token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }

        # Find NETWORK_INSIGHT adapter
        req = urllib.request.Request(
            f'https://{ops_host}/suite-api/api/adapters?adapterKindKey=NETWORK_INSIGHT'
        )
        for k, v in ops_headers.items():
            req.add_header(k, v)
        resp = urllib.request.urlopen(req, context=ctx)
        adapters = json.loads(resp.read())
        adapter_list = adapters.get('adapterInstancesInfoDto', [])
        if not adapter_list:
            lsf.write_output('No NETWORK_INSIGHT adapter found')
            return False

        adapter = adapter_list[0]
        adapter_id = adapter['id']
        cred_id = adapter.get('credentialInstanceId', '')

        # Create or find an editable credential
        req = urllib.request.Request(
            f'https://{ops_host}/suite-api/api/credentials'
        )
        for k, v in ops_headers.items():
            req.add_header(k, v)
        resp = urllib.request.urlopen(req, context=ctx)
        all_creds = json.loads(resp.read()).get('credentialInstances', [])

        editable_cred_id = None
        for c in all_creds:
            if c.get('adapterKindKey') == 'NETWORK_INSIGHT' and c.get('editable'):
                editable_cred_id = c['id']
                break

        if editable_cred_id is None:
            new_cred = {
                "name": "ops-ni integration credential fixed",
                "adapterKindKey": "NETWORK_INSIGHT",
                "credentialKindKey": "NETWORK_INSIGHT_CREDENTIAL",
                "fields": [
                    {"name": "USERNAME", "value": svc_account},
                    {"name": "PASSWORD", "value": password}
                ]
            }
            body = json.dumps(new_cred).encode()
            req = urllib.request.Request(
                f'https://{ops_host}/suite-api/api/credentials',
                data=body, method='POST'
            )
            for k, v in ops_headers.items():
                req.add_header(k, v)
            resp = urllib.request.urlopen(req, context=ctx)
            created_cred = json.loads(resp.read())
            editable_cred_id = created_cred['id']
            lsf.write_output(f'Created new editable credential: {editable_cred_id}')
        else:
            update_cred = {
                "id": editable_cred_id,
                "name": "ops-ni integration credential fixed",
                "adapterKindKey": "NETWORK_INSIGHT",
                "credentialKindKey": "NETWORK_INSIGHT_CREDENTIAL",
                "fields": [
                    {"name": "USERNAME", "value": svc_account},
                    {"name": "PASSWORD", "value": password}
                ]
            }
            body = json.dumps(update_cred).encode()
            req = urllib.request.Request(
                f'https://{ops_host}/suite-api/api/credentials',
                data=body, method='PUT'
            )
            for k, v in ops_headers.items():
                req.add_header(k, v)
            resp = urllib.request.urlopen(req, context=ctx)
            lsf.write_output(f'Updated editable credential: {editable_cred_id}')

        # Assign credential to adapter
        if cred_id != editable_cred_id:
            req = urllib.request.Request(
                f'https://{ops_host}/suite-api/api/adapters/{adapter_id}'
            )
            for k, v in ops_headers.items():
                req.add_header(k, v)
            resp = urllib.request.urlopen(req, context=ctx)
            adapter_data = json.loads(resp.read())

            adapter_data['credentialInstanceId'] = editable_cred_id
            if 'collectorGroupId' in adapter_data:
                del adapter_data['collectorGroupId']

            body = json.dumps(adapter_data).encode()
            req = urllib.request.Request(
                f'https://{ops_host}/suite-api/api/adapters',
                data=body, method='PUT'
            )
            for k, v in ops_headers.items():
                req.add_header(k, v)
            resp = urllib.request.urlopen(req, context=ctx)
            lsf.write_output('Adapter credential updated')

        # Fix Fleet Management password for opsnet admin@local
        try:
            lsf.write_output('Fixing Fleet Management password for opsnet-01a admin@local...')
            ops_headers['X-vRealizeOps-API-use-unsupported'] = 'true'
            
            # Query password accounts
            req = urllib.request.Request(
                f'https://{ops_host}/suite-api/internal/passwordmanagement/passwords/query',
                data=json.dumps({"searchCriteria": {}}).encode(),
                method='POST'
            )
            for k, v in ops_headers.items():
                req.add_header(k, v)
            resp = urllib.request.urlopen(req, context=ctx)
            accounts = json.loads(resp.read()).get('vcfPasswordAccounts', [])
            
            target_key = None
            for acc in accounts:
                if acc.get('applianceFqdn') == 'opsnet-01a.site-a.vcf.lab' and acc.get('userName') == 'admin@local':
                    if acc.get('status') == 'DISCONNECTED':
                        target_key = acc.get('passwordResourceKey')
                    break
                    
            if target_key:
                payload = json.dumps({
                    "password": password,
                    "userName": "admin@local"
                }).encode()
                req = urllib.request.Request(
                    f'https://{ops_host}/suite-api/internal/passwordmanagement/passwords/{target_key}/update',
                    data=payload,
                    method='PUT'
                )
                for k, v in ops_headers.items():
                    req.add_header(k, v)
                resp = urllib.request.urlopen(req, context=ctx)
                lsf.write_output('Fleet Management password update task started')
        except Exception as e:
            lsf.write_output(f'Fleet Management password update failed: {e}')

        lsf.write_output('VCF Operations opsnet fix complete')
        return True

    except Exception as e:
        lsf.write_output(f'VCF Operations adapter update failed: {e}')
        return False


try:
    fix_opsnet_service_account()
except Exception as e:
    lsf.write_output(f'opsnet fix exception: {e}')


for si in lsf.sis:
    connect.Disconnect(si)

lsf.write_output(f'{sys.argv[0]} finished.')
 