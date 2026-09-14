#!/bin/zsh
set -eu
meter_source_dir="${0:A:h}"
python3 "$meter_source_dir/scripts/install.py"
read -r "?按回车关闭窗口"
