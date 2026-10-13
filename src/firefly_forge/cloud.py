# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reviewable VM recipes; rendering never contacts a cloud or applies infrastructure."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any, Literal

from cryptography.hazmat.primitives.serialization import load_ssh_public_key
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pyfly.container import service

from firefly_forge.configuration import validate_endpoint
from firefly_forge.operations import DeploymentManager, DeploymentTarget
from firefly_forge.store import Store

Provider = Literal["aws", "azure", "gcp", "digitalocean", "vps", "onprem"]
IMAGE = r"^[a-z0-9][a-z0-9.:/-]*@sha256:[a-f0-9]{64}$"
SCHEMAS: dict[str, dict[str, Any]] = {
    "aws": {
        "source": "hashicorp/aws",
        "version": "~> 6.68.0",
        "required": ["region", "vpc_id", "subnet_id", "image_id", "state_bucket"],
        "defaults": {"size": "t3.small", "state_key": "forge/inventory.tfstate"},
        "credentials": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"],
    },
    "azure": {
        "source": "hashicorp/azurerm",
        "version": "~> 5.9.0",
        "required": ["region", "subscription_id", "state_resource_group", "state_storage_account", "state_container"],
        "defaults": {"size": "Standard_B2s", "state_key": "forge/inventory.tfstate"},
        "credentials": ["ARM_CLIENT_ID", "ARM_CLIENT_SECRET", "ARM_TENANT_ID", "ARM_SUBSCRIPTION_ID"],
    },
    "gcp": {
        "source": "hashicorp/google",
        "version": "~> 8.6.0",
        "required": ["project", "region", "zone", "state_bucket"],
        "defaults": {
            "size": "e2-small",
            "image_id": "ubuntu-os-cloud/ubuntu-2404-lts-amd64",
            "state_key": "forge/inventory",
        },
        "credentials": ["GOOGLE_APPLICATION_CREDENTIALS"],
    },
    "digitalocean": {
        "source": "digitalocean/digitalocean",
        "version": "~> 2.106.0",
        "required": ["region", "state_bucket", "state_region", "state_endpoint", "ssh_key_id"],
        "defaults": {"size": "s-1vcpu-2gb", "image_id": "ubuntu-24-04-x64", "state_key": "forge/inventory.tfstate"},
        "credentials": ["DIGITALOCEAN_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
    },
    "vps": {"required": ["host"], "defaults": {}, "credentials": ["SSH_AUTH_SOCK"]},
    "onprem": {"required": ["host"], "defaults": {}, "credentials": ["SSH_AUTH_SOCK"]},
}


class CloudRecipeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Provider
    repository_id: str = Field(min_length=1, max_length=100)
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    image: str = Field(pattern=IMAGE, max_length=300)
    previous_image: str | None = Field(default=None, pattern=IMAGE, max_length=300)
    domain: str = Field(pattern=r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", max_length=253)
    health_path: str = Field(default="/health", pattern=r"^/[A-Za-z0-9._/-]*$", max_length=200)
    container_port: int = Field(default=8080, ge=1024, le=65535)
    ssh_user: str = Field(default="ubuntu", pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    ssh_public_key: str = Field(min_length=30, max_length=16000)
    admin_cidr: str
    credential_profile: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    config: dict[str, str] = Field(max_length=20)

    @field_validator("ssh_public_key")
    @classmethod
    def public_key(cls, value: str) -> str:
        if not re.fullmatch(
            r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+(?: [A-Za-z0-9@._-]+)?", value
        ):
            raise ValueError("Use a single public SSH key; private keys are never recipe inputs")
        try:
            load_ssh_public_key(value.encode())
        except ValueError as exc:
            raise ValueError("Use a valid OpenSSH public key") from exc
        return value

    @field_validator("admin_cidr")
    @classmethod
    def restricted_ssh(cls, value: str) -> str:
        network = ipaddress.ip_network(value, strict=True)
        if network.version != 4 or network.prefixlen == 0:
            raise ValueError("SSH requires a restricted IPv4 administration CIDR")
        return str(network)

    @model_validator(mode="after")
    def provider_configuration(self) -> CloudRecipeInput:
        schema = SCHEMAS[self.provider]
        allowed = set(schema["required"]) | set(schema["defaults"])
        if set(self.config) - allowed or set(schema["required"]) - self.config.keys():
            raise ValueError("Supply exactly the supported provider fields; credentials belong in a private profile")
        for key, value in self.config.items():
            if not value or len(value) > 300 or not re.fullmatch(r"[A-Za-z0-9._:/-]+", value):
                raise ValueError("Cloud configuration fields must be bounded nonsecret identifiers")
            if key == "state_endpoint":
                validate_endpoint(value)
        if self.provider in {"vps", "onprem"}:
            host = self.config["host"]
            if host.startswith("-") or "/" in host or ":" in host:
                raise ValueError("Use an IPv4 address or DNS hostname for the dedicated SSH host")
        if self.provider == "aws" and not self.config["image_id"].startswith("ami-"):
            raise ValueError("AWS requires an explicit Ubuntu AMI ID")
        return self


def catalog() -> list[dict[str, Any]]:
    names = {
        "aws": "AWS EC2",
        "azure": "Azure Linux VM",
        "gcp": "Google Compute Engine",
        "digitalocean": "DigitalOcean Droplet",
        "vps": "Existing VPS",
        "onprem": "Dedicated on-premises host",
    }
    return [
        {
            "id": provider,
            "name": names[provider],
            "status": "recipe_available",
            "template_version": "1.0.0",
            "tool": "OpenTofu + Ansible" if "source" in schema else "Ansible",
            "provider_source": schema.get("source"),
            "provider_version": schema.get("version"),
            "fields": [
                {"key": key, "required": key in schema["required"], "default": schema["defaults"].get(key, "")}
                for key in [*schema["required"], *schema["defaults"]]
            ],
            "credential_names": schema["credentials"],
            "infrastructure_applied": False,
        }
        for provider, schema in SCHEMAS.items()
    ]


def _terraform(request: CloudRecipeInput) -> dict[str, Any]:
    p = request.provider
    n = request.name + "-" + hashlib.sha256(request.repository_id.encode()).hexdigest()[:8]
    c = {**SCHEMAS[p]["defaults"], "state_key": f"forge/{request.repository_id}/{n}.tfstate", **request.config}
    resources: dict[str, Any] = {}
    provider: dict[str, Any]
    backend: dict[str, Any]
    if p == "aws":
        provider = {"aws": {"region": c["region"]}}
        backend = {
            "s3": {
                "bucket": c["state_bucket"],
                "key": c["state_key"],
                "region": c["region"],
                "encrypt": True,
                "use_lockfile": True,
            }
        }
        resources = {
            "aws_key_pair": {"operator": {"key_name": n, "public_key": request.ssh_public_key}},
            "aws_security_group": {
                "app": {
                    "name_prefix": n + "-",
                    "vpc_id": c["vpc_id"],
                    "ingress": [
                        {"from_port": 22, "to_port": 22, "protocol": "tcp", "cidr_blocks": [request.admin_cidr]},
                        *[
                            {"from_port": port, "to_port": port, "protocol": "tcp", "cidr_blocks": ["0.0.0.0/0"]}
                            for port in [80, 443]
                        ],
                    ],
                    "egress": [{"from_port": 0, "to_port": 0, "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"]}],
                }
            },
            "aws_instance": {
                "app": {
                    "ami": c["image_id"],
                    "instance_type": c["size"],
                    "subnet_id": c["subnet_id"],
                    "associate_public_ip_address": True,
                    "key_name": "${aws_key_pair.operator.key_name}",
                    "vpc_security_group_ids": ["${aws_security_group.app.id}"],
                    "metadata_options": {"http_tokens": "required"},
                    "root_block_device": {"encrypted": True, "volume_size": 30},
                    "tags": {"Name": n},
                    "user_data": "#cloud-config\n"
                    + json.dumps(
                        {
                            "users": [
                                "default",
                                {
                                    "name": request.ssh_user,
                                    "sudo": "ALL=(ALL) NOPASSWD:ALL",
                                    "shell": "/bin/bash",
                                    "ssh_authorized_keys": [request.ssh_public_key],
                                },
                            ]
                        }
                    ),
                }
            },
        }
        resources["aws_security_group"]["app"].pop("ingress")
        resources["aws_security_group"]["app"].pop("egress")
        resources["aws_vpc_security_group_ingress_rule"] = {
            name: {
                "security_group_id": "${aws_security_group.app.id}",
                "ip_protocol": "tcp",
                "from_port": port,
                "to_port": port,
                "cidr_ipv4": request.admin_cidr if port == 22 else "0.0.0.0/0",
            }
            for name, port in [("ssh", 22), ("http", 80), ("https", 443)]
        }
        resources["aws_vpc_security_group_egress_rule"] = {
            "updates": {
                "security_group_id": "${aws_security_group.app.id}",
                "ip_protocol": "-1",
                "cidr_ipv4": "0.0.0.0/0",
            }
        }
        host = "${aws_instance.app.public_ip}"
    elif p == "azure":
        provider = {"azurerm": {"features": {}, "subscription_id": c["subscription_id"]}}
        backend = {
            "azurerm": {
                "resource_group_name": c["state_resource_group"],
                "storage_account_name": c["state_storage_account"],
                "container_name": c["state_container"],
                "key": c["state_key"],
                "use_azuread_auth": True,
            }
        }
        common = {"location": c["region"], "resource_group_name": "${azurerm_resource_group.app.name}"}
        resources = {
            "azurerm_resource_group": {"app": {"name": n + "-forge", "location": c["region"]}},
            "azurerm_virtual_network": {"app": {**common, "name": n, "address_space": ["10.42.0.0/16"]}},
            "azurerm_subnet": {
                "app": {
                    "resource_group_name": common["resource_group_name"],
                    "name": n,
                    "virtual_network_name": "${azurerm_virtual_network.app.name}",
                    "address_prefixes": ["10.42.1.0/24"],
                }
            },
            "azurerm_network_security_group": {
                "app": {
                    **common,
                    "name": n,
                    "security_rule": [
                        {
                            "name": "ssh",
                            "priority": 100,
                            "direction": "Inbound",
                            "access": "Allow",
                            "protocol": "Tcp",
                            "source_port_range": "*",
                            "destination_port_range": "22",
                            "source_address_prefix": request.admin_cidr,
                            "destination_address_prefix": "*",
                        },
                        {
                            "name": "web",
                            "priority": 110,
                            "direction": "Inbound",
                            "access": "Allow",
                            "protocol": "Tcp",
                            "source_port_range": "*",
                            "destination_port_ranges": ["80", "443"],
                            "source_address_prefix": "*",
                            "destination_address_prefix": "*",
                        },
                    ],
                }
            },
            "azurerm_public_ip": {"app": {**common, "name": n, "allocation_method": "Static", "sku": "Standard"}},
            "azurerm_network_interface": {
                "app": {
                    **common,
                    "name": n,
                    "ip_configuration": {
                        "name": "app",
                        "subnet_id": "${azurerm_subnet.app.id}",
                        "private_ip_address_allocation": "Dynamic",
                        "public_ip_address_id": "${azurerm_public_ip.app.id}",
                    },
                }
            },
            "azurerm_network_interface_security_group_association": {
                "app": {
                    "network_interface_id": "${azurerm_network_interface.app.id}",
                    "network_security_group_id": "${azurerm_network_security_group.app.id}",
                }
            },
            "azurerm_linux_virtual_machine": {
                "app": {
                    **common,
                    "name": n,
                    "size": c["size"],
                    "admin_username": request.ssh_user,
                    "disable_password_authentication": True,
                    "network_interface_ids": ["${azurerm_network_interface.app.id}"],
                    "admin_ssh_key": {"username": request.ssh_user, "public_key": request.ssh_public_key},
                    "os_disk": {"caching": "ReadWrite", "storage_account_type": "Standard_LRS"},
                    "source_image_reference": {
                        "publisher": "Canonical",
                        "offer": "ubuntu-24_04-lts",
                        "sku": "server",
                        "version": "latest",
                    },
                }
            },
        }
        rules = resources["azurerm_network_security_group"]["app"].pop("security_rule")
        resources["azurerm_network_security_rule"] = {
            rule["name"]: {
                **rule,
                "resource_group_name": common["resource_group_name"],
                "network_security_group_name": "${azurerm_network_security_group.app.name}",
            }
            for rule in rules
        }
        host = "${azurerm_public_ip.app.ip_address}"
    elif p == "gcp":
        provider = {"google": {"project": c["project"], "region": c["region"], "zone": c["zone"]}}
        backend = {"gcs": {"bucket": c["state_bucket"], "prefix": c["state_key"]}}
        resources = {
            "google_compute_network": {"app": {"name": n, "auto_create_subnetworks": False}},
            "google_compute_subnetwork": {
                "app": {
                    "name": n,
                    "region": c["region"],
                    "ip_cidr_range": "10.42.0.0/24",
                    "network": "${google_compute_network.app.id}",
                }
            },
            "google_compute_firewall": {
                "ssh": {
                    "name": n + "-ssh",
                    "network": "${google_compute_network.app.name}",
                    "source_ranges": [request.admin_cidr],
                    "target_tags": [n],
                    "allow": {"protocol": "tcp", "ports": ["22"]},
                },
                "web": {
                    "name": n + "-web",
                    "network": "${google_compute_network.app.name}",
                    "source_ranges": ["0.0.0.0/0"],
                    "target_tags": [n],
                    "allow": {"protocol": "tcp", "ports": ["80", "443"]},
                },
            },
            "google_compute_instance": {
                "app": {
                    "name": n,
                    "machine_type": c["size"],
                    "tags": [n],
                    "boot_disk": {"initialize_params": {"image": c["image_id"], "size": 30}},
                    "network_interface": {"subnetwork": "${google_compute_subnetwork.app.id}", "access_config": {}},
                    "metadata": {
                        "ssh-keys": request.ssh_user + ":" + request.ssh_public_key,
                        "block-project-ssh-keys": "true",
                    },
                    "shielded_instance_config": {"enable_secure_boot": True},
                }
            },
        }
        host = "${google_compute_instance.app.network_interface[0].access_config[0].nat_ip}"
    else:
        provider = {"digitalocean": {}}
        backend = {
            "s3": {
                "bucket": c["state_bucket"],
                "key": c["state_key"],
                "region": c["state_region"],
                "endpoints": {"s3": c["state_endpoint"]},
                "skip_credentials_validation": True,
                "skip_metadata_api_check": True,
                "skip_s3_checksum": True,
                "skip_region_validation": True,
                "skip_requesting_account_id": True,
                "use_path_style": True,
                "use_lockfile": True,
            }
        }
        resources = {
            "digitalocean_droplet": {
                "app": {
                    "name": n,
                    "region": c["region"],
                    "size": c["size"],
                    "image": c["image_id"],
                    "ssh_keys": [c["ssh_key_id"]],
                    "monitoring": True,
                    "backups": True,
                    "user_data": "#cloud-config\n"
                    + json.dumps(
                        {
                            "users": [
                                "default",
                                {
                                    "name": request.ssh_user,
                                    "sudo": "ALL=(ALL) NOPASSWD:ALL",
                                    "shell": "/bin/bash",
                                    "ssh_authorized_keys": [request.ssh_public_key],
                                },
                            ]
                        }
                    ),
                }
            },
            "digitalocean_firewall": {
                "app": {
                    "name": n,
                    "droplet_ids": ["${digitalocean_droplet.app.id}"],
                    "inbound_rule": [
                        {"protocol": "tcp", "port_range": "22", "source_addresses": [request.admin_cidr]},
                        *[
                            {"protocol": "tcp", "port_range": str(port), "source_addresses": ["0.0.0.0/0"]}
                            for port in [80, 443]
                        ],
                    ],
                    "outbound_rule": [
                        {"protocol": "tcp", "port_range": "1-65535", "destination_addresses": ["0.0.0.0/0"]},
                        {"protocol": "udp", "port_range": "1-65535", "destination_addresses": ["0.0.0.0/0"]},
                    ],
                }
            },
        }
        host = "${digitalocean_droplet.app.ipv4_address}"
    return {
        "terraform": {
            "required_version": ">= 1.11.0, < 2.0.0",
            "required_providers": {
                SCHEMAS[p]["source"].split("/")[-1]: {"source": SCHEMAS[p]["source"], "version": SCHEMAS[p]["version"]}
            },
            "backend": backend,
        },
        "provider": provider,
        "resource": resources,
        "output": {"host": {"value": host}},
    }


def _playbook(request: CloudRecipeInput) -> list[dict[str, Any]]:
    n, d, port = request.name, request.domain, request.container_port
    environment = f"/etc/forge/{n}.env"
    certificate = f"/etc/letsencrypt/live/{d}"
    tasks = [
        {"name": "Wait for authenticated SSH", "ansible.builtin.wait_for_connection": {"timeout": 300}},
        {
            "name": "Require externally managed application secrets",
            "ansible.builtin.stat": {"path": environment},
            "register": "application_secrets",
            "no_log": True,
        },
        {
            "name": "Validate private secrets file",
            "ansible.builtin.assert": {
                "that": [
                    "application_secrets.stat.exists",
                    "application_secrets.stat.isreg",
                    "application_secrets.stat.mode == '0600'",
                    "application_secrets.stat.pw_name == 'root'",
                ]
            },
            "no_log": True,
        },
        {
            "name": "Require externally managed TLS certificate",
            "ansible.builtin.stat": {"path": certificate + "/fullchain.pem", "follow": True},
            "register": "tls_certificate",
        },
        {
            "name": "Validate TLS setup",
            "ansible.builtin.assert": {"that": ["tls_certificate.stat.exists", "tls_certificate.stat.isreg"]},
        },
        {
            "name": "Install distro runtime packages",
            "ansible.builtin.apt": {"name": ["docker.io", "nginx"], "state": "present", "update_cache": True},
        },
        {"name": "Start Docker", "ansible.builtin.service": {"name": "docker", "state": "started", "enabled": True}},
        {
            "name": "Pull the reviewed immutable image",
            "ansible.builtin.command": {
                "argv": ["docker", "pull", "{{ desired_image }}"],
                "expand_argument_vars": False,
            },
            "no_log": True,
        },
        {
            "name": "Inspect image runtime user",
            "ansible.builtin.command": {"argv": ["docker", "image", "inspect", "{{ desired_image }}"]},
            "register": "image_metadata",
            "changed_when": False,
            "no_log": True,
        },
        {
            "name": "Require a nonroot container image",
            "ansible.builtin.assert": {
                "that": [
                    "(image_metadata.stdout | from_json)[0].Config.User not in ['', '0', 'root', '0:0', 'root:root']"
                ]
            },
            "no_log": True,
        },
        {
            "name": "Inspect the current container",
            "ansible.builtin.command": {"argv": ["docker", "container", "inspect", n]},
            "register": "current_container",
            "failed_when": False,
            "changed_when": False,
            "no_log": True,
        },
        {
            "name": "Replace the named application container",
            "ansible.builtin.command": {"argv": ["docker", "rm", "--force", n]},
            "when": "current_container.rc == 0",
        },
        {
            "name": "Start confined application container",
            "ansible.builtin.command": {
                "argv": [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    n,
                    "--restart",
                    "unless-stopped",
                    "--security-opt",
                    "no-new-privileges",
                    "--cap-drop",
                    "ALL",
                    "--read-only",
                    "--tmpfs",
                    "/tmp",
                    "--pids-limit",
                    "256",
                    "--memory",
                    "512m",
                    "--env-file",
                    environment,
                    "--publish",
                    f"127.0.0.1:8080:{port}",
                    "{{ desired_image }}",
                ],
                "expand_argument_vars": False,
            },
            "no_log": True,
        },
        {
            "name": "Verify local application behavior",
            "ansible.builtin.uri": {"url": f"http://127.0.0.1:8080{request.health_path}", "status_code": 200},
            "register": "local_health",
            "until": "local_health.status == 200",
            "retries": 12,
            "delay": 5,
        },
        {
            "name": "Configure the TLS reverse proxy",
            "ansible.builtin.copy": {
                "dest": f"/etc/nginx/conf.d/forge-{n}.conf",
                "mode": "0644",
                "content": (
                    f"server {{ listen 80; server_name {d}; return 308 https://$host$request_uri; }}\n"
                    f"server {{ listen 443 ssl; server_name {d}; ssl_certificate {certificate}/fullchain.pem; "
                    f"ssl_certificate_key {certificate}/privkey.pem; ssl_protocols TLSv1.2 TLSv1.3; "
                    "location / { proxy_pass http://127.0.0.1:8080; proxy_set_header Host $host; "
                    "proxy_set_header X-Forwarded-Proto https; } }\n"
                ),
            },
        },
        {
            "name": "Validate nginx configuration",
            "ansible.builtin.command": {"argv": ["nginx", "-t"]},
            "changed_when": False,
        },
        {
            "name": "Enable and reload TLS",
            "ansible.builtin.service": {"name": "nginx", "enabled": True, "state": "restarted"},
        },
    ]
    return [
        {
            "name": "Deploy reviewed immutable application",
            "hosts": "all",
            "become": True,
            "gather_facts": False,
            "vars": {"desired_image": request.image},
            "tasks": tasks,
        }
    ]


@service
class CloudRecipes:
    def __init__(self, store: Store, deployments: DeploymentManager | None = None) -> None:
        self.store, self.deployments = store, deployments
        self.directory = store.configuration.directory / "cloud-recipes"
        self.directory.mkdir(exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError("Cloud recipes require a private real directory")
        self.directory.chmod(0o700)

    def catalog(self) -> list[dict[str, Any]]:
        return catalog()

    async def render(self, request: CloudRecipeInput) -> dict[str, Any]:
        await self.store.repository(request.repository_id)
        files = {"deploy.playbook.json": json.dumps(_playbook(request), indent=2) + "\n"}
        commands = []
        if request.provider in {"vps", "onprem"}:
            inventory = {
                "all": {
                    "hosts": {"application": {"ansible_host": request.config["host"], "ansible_user": request.ssh_user}}
                }
            }
            files["inventory.json"] = json.dumps(inventory, indent=2)
        else:
            files["main.tf.json"] = json.dumps(_terraform(request), indent=2) + "\n"
            for arguments in [
                ["init", "-input=false"],
                ["validate"],
                ["plan", "-input=false", "-out=reviewed.tfplan"],
                ["apply", "-input=false", "reviewed.tfplan"],
            ]:
                commands.append({"argv": ["tofu", "-chdir=.forge-deployment", *arguments], "timeout": 1800})
            files["inventory.py"] = (
                "import json,subprocess\nfrom pathlib import Path\n"
                "host=json.loads(subprocess.check_output(['tofu','-chdir=.forge-deployment','output','-json','host']))\n"
                "if not isinstance(host,str) or not host or any(c in host for c in '\\n\\r /'): "
                "raise ValueError('Invalid VM host')\n"
                "inventory={'all':{'hosts':{'application':{'ansible_host':host,"
                f"'ansible_user':{request.ssh_user!r}" + "}}}}\n"
                "Path('.forge-deployment/inventory.json').write_text(json.dumps(inventory))\n"
            )
            commands.append({"argv": ["python3", ".forge-deployment/inventory.py"], "timeout": 60})
        ansible = [
            "ansible-playbook",
            "-i",
            ".forge-deployment/inventory.json",
            "--ssh-common-args",
            "-o StrictHostKeyChecking=yes",
            ".forge-deployment/deploy.playbook.json",
        ]
        commands.append({"argv": ansible, "timeout": 1800})
        health = {
            "argv": [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--retry",
                "5",
                "--retry-connrefused",
                "--max-time",
                "30",
                "--output",
                "/dev/null",
                f"https://{request.domain}{request.health_path}",
            ],
            "timeout": 300,
        }
        rollback = []
        if request.previous_image:
            files["rollback-vars.json"] = json.dumps({"desired_image": request.previous_image})
            if request.provider not in {"vps", "onprem"}:
                rollback.extend(
                    [
                        {"argv": ["tofu", "-chdir=.forge-deployment", "init", "-input=false"], "timeout": 1800},
                        {"argv": ["python3", ".forge-deployment/inventory.py"], "timeout": 60},
                    ]
                )
            rollback.append(
                {"argv": [*ansible, "--extra-vars", "@.forge-deployment/rollback-vars.json"], "timeout": 1800}
            )
        target = {
            "name": request.name,
            "repository_id": request.repository_id,
            "commands": commands,
            "verification": [health],
            "rollback_commands": rollback,
            "rollback_verification": [health] if rollback else [],
            "recipe_files": files,
            "credential_profile": request.credential_profile,
        }
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        return {
            "id": uuid.uuid4().hex,
            "provider": request.provider,
            "files": files,
            "target": target,
            "content_digest": digest,
            "requirements": [
                "Dedicated Ubuntu 24.04 host with sudo SSH access and verified host key",
                f"External root-owned 0600 secrets file /etc/forge/{request.name}.env",
                "Externally managed TLS certificate and private key under /etc/letsencrypt/live/DOMAIN",
                "DNS domain resolves to the target; valid certificate trusted by deployment host",
                "Nonroot stateless image supporting read-only rootfs and /tmp; external persistence",
                "Remote registry authentication installed separately on the VM",
                "Cloud remote state backend already exists; state permissions and lock support configured",
                "Operator-selected credential profile; coding workers never receive deployment credentials",
            ],
            "validation": {"state": "rendered", "infrastructure_applied": False, "cloud_account_verified": False},
        }

    async def save(self, request: CloudRecipeInput) -> dict[str, Any]:
        result = await self.render(request)
        self.store.configuration._write(self.directory / f"{result['id']}.json", result)
        return {**result, "saved": True}

    def _load(self, identifier: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("Invalid recipe identifier")
        result = self.store.configuration._read(self.directory / f"{identifier}.json")
        if not result:
            raise ValueError("Save a cloud recipe before exporting or configuring it")
        if sum(len(value.encode()) for value in result["files"].values()) > 1_000_000:
            raise ValueError("Recipe exceeds export bounds")
        return result

    async def export(self, identifier: str) -> Path:
        result = self._load(identifier)
        target = self.directory / f"{identifier}.zip"
        if target.is_symlink() or (target.exists() and (target.stat().st_nlink != 1 or not target.is_file())):
            raise ValueError("Recipe archive must be a regular private file")
        descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix=".export-")
        try:
            with os.fdopen(descriptor, "wb") as output, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, value in result["files"].items():
                    path = Path(name)
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError("Invalid recipe export path")
                    archive.writestr(name, value)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return target

    async def configure_target(self, identifier: str, target_id: str) -> dict[str, Any]:
        if not self.deployments:
            raise ValueError("Deployment service is unavailable")
        result = self._load(identifier)
        if not result["target"].get("credential_profile"):
            raise ValueError("Select an explicit deployment credential profile before enabling this target")
        return await self.deployments.configure_target(target_id, DeploymentTarget.model_validate(result["target"]))
