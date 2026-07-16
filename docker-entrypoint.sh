#!/bin/sh
set -eu

config_file="${MPT_CONFIG_FILE:-/MoneyPrinterTurbo/config/config.toml}"
config_dir="$(dirname "$config_file")"
storage_dir="/MoneyPrinterTurbo/storage"

mkdir -p "$config_dir" "$storage_dir"

if [ "$(id -u)" = "0" ]; then
    chown -R mpt:mpt "$config_dir" "$storage_dir"
    exec gosu mpt "$@"
fi

exec "$@"
