#!/usr/bin/env bash
set -Eeuo pipefail
result_dir="${GITHUB_WORKSPACE:-$PWD}/test-results/lc4ri-operationhub"
mkdir -p "$result_dir"
exec > >(tee "$result_dir/run.log") 2>&1
source tests/lc4ri/guests/qemu-guests.sh
bridge=cwh-e2e-br0
subnet=10.1.1.0/24
gateway=10.1.1.1
master_ip=10.1.1.9
worker_ip=10.1.1.11
controller=cwh-lc4ri-controller
cleanup() {
  result=$?
  trap - EXIT
  set +e
  if ((result != 0)); then
    timeout 20s ssh -F "$private_dir/ssh_config" "centos@$master_ip" 'free -h; df -h; ps -eo pid,ppid,comm,rss,%cpu,etime,stat --sort=-rss; sudo journalctl -k --no-pager' > "$result_dir/operationhub-guest-resources.txt" 2>&1
  fi
  run_with_timeout 60s docker logs "$controller" > "$result_dir/controller.log" 2>&1
  run_with_timeout 2m docker cp "$controller:/home/jovyan/work/.log" "$result_dir/lc-logs"
  run_with_timeout 2m docker cp "$controller:/home/jovyan/work/hosts" "$result_dir/"
  run_with_timeout 60s docker rm -f "$controller"
  for host in "$master_ip" 10.1.1.10 10.1.1.11 10.1.1.12; do
    timeout 20s ssh -F "$private_dir/ssh_config" "centos@$host" 'sudo journalctl --no-pager -n 400; sudo findmnt' > "$result_dir/$host-state.txt" 2>&1
  done
  timeout 20s ssh -F "$private_dir/ssh_config" "centos@$master_ip" 'cd /opt/ophub && sudo docker compose logs' > "$result_dir/operationhub-services.log" 2>&1
  timeout 30s ssh -F "$private_dir/ssh_config" "centos@$master_ip" 'sudo tar -czf - -C /home/ohadmin/notebooks .log' > "$result_dir/user-lc-logs.tar.gz" 2> "$result_dir/user-lc-logs-capture.txt"
  qemu_host_diagnostics
  qemu_cleanup
  sudo chown -R "$(id -u):$(id -g)" "$result_dir"
  exit "$result"
}
trap cleanup EXIT
export CWH_E2E_AWS_CONSOLE=1
qemu_boot_guests "operationhub:$master_ip:8192:40G" "master:10.1.1.10:3072:40G" "worker1:10.1.1.11:2048:40G" "worker2:10.1.1.12:2048:40G"
for host in "${qemu_addresses[@]}"; do
  run_with_timeout 10m ssh -F "$private_dir/ssh_config" "centos@$host" 'sudo dnf install -y python3'
done
export LC4RI_AWS_HOST=$gateway
base_image=$(awk '$1 == "FROM" {print $2; exit}' tests/lc4ri/controller/Dockerfile)
run_with_timeout 15m docker pull "$base_image"
run_with_timeout 20m docker build -t cwh-lc4ri-controller tests/lc4ri/controller
export LC4RI_TEST_TOKEN
LC4RI_TEST_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
run_with_timeout 10m docker run -d --name "$controller" --network host \
  -e ANSIBLE_PYTHON_INTERPRETER=/usr/bin/python3 -e ANSIBLE_DEPRECATION_WARNINGS=False \
  -e AWS_ACCESS_KEY_ID=testing -e AWS_SECRET_ACCESS_KEY=testing \
  -e AWS_DEFAULT_REGION=ap-northeast-1 -e AWS_EC2_METADATA_DISABLED=true \
  -e AWS_ENDPOINT_URL="http://$gateway:18503" \
  cwh-lc4ri-controller start-notebook.sh \
  --ServerApp.ip=0.0.0.0 --ServerApp.port=18888 --ServerApp.token="$LC4RI_TEST_TOKEN" \
  --ServerApp.root_dir=/home/jovyan/work --ServerApp.default_url=/lab --LabApp.expose_app_in_browser=True
run_with_timeout 2m docker cp "$private_dir/id_ed25519" "$controller:/home/jovyan/.ssh/ansible_id_rsa"
run_with_timeout 2m docker cp "$private_dir/known_hosts" "$controller:/home/jovyan/.ssh/known_hosts"
run_with_timeout 2m docker exec -u root "$controller" sh -c 'chmod 0600 /home/jovyan/.ssh/*; chown -R 1000:100 /home/jovyan/.ssh'
run_with_timeout 2m docker exec -u root "$controller" sh -c 'printf "Host *\n  BatchMode no\n  StrictHostKeyChecking ask\n  IdentitiesOnly yes\n  IdentityFile ~/.ssh/ansible_id_rsa\n" > /home/jovyan/.ssh/config; chown 1000:100 /home/jovyan/.ssh/config; chmod 0600 /home/jovyan/.ssh/config'
run_with_timeout 60s docker image inspect "$base_image" --format '{{json .RepoDigests}}' > "$result_dir/lc4ri-image.json"
run_with_timeout 2m docker exec "$controller" python3 -m pip freeze > "$result_dir/controller-packages.txt"
run_with_timeout 2m docker cp "$private_dir/id_ed25519.pub" "$controller:/home/jovyan/.ssh/ansible_id_rsa.pub"
run_with_timeout 2m docker cp image "$controller:/home/jovyan/work/image"
run_with_timeout 2m docker cp coursewarehub "$controller:/home/jovyan/work/coursewarehub"
run_with_timeout 2m docker exec -i -u root "$controller" sh -c 'mkdir -p /home/jovyan/.aws; cat > /home/jovyan/.aws/config' <<EOF
[default]
region = ap-northeast-1
output = json
endpoint_url = http://$gateway:18503
EOF
run_with_timeout 2m docker exec -i -u root "$controller" sh -c 'cat > /home/jovyan/.aws/credentials' <<'EOF'
[default]
aws_access_key_id = testing
aws_secret_access_key = testing
EOF
run_with_timeout 2m docker exec -u root "$controller" chown -R 1000:100 /home/jovyan/.aws /home/jovyan/.ssh /home/jovyan/work
python3 tests/lc4ri/notebook_ui.py --result-dir "$result_dir" --master "$master_ip" --worker "$worker_ip" --operationhub
run_with_timeout 2m docker exec "$controller" sh -c 'test -n "$(find /home/jovyan/work/.log -type f -name "*.log" -print -quit)"'
