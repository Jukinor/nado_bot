# Nado build without PyYAML

Эта сборка полностью убирает `pydantic-settings` и зависимость от `PyYAML`, потому что текущий проект использует `.env`, а не YAML-конфиги.[file:381][web:471]

## Установка

```bash
apt update
apt install -y python3 python3-venv python3-pip python-is-python3 build-essential pkg-config libssl-dev cargo rustc
cd /opt/nado_nopyyaml_build
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

## Почему это должно ставиться легче

`pydantic-settings` и `PyYAML` удалены, а конфиг теперь читается через `python-dotenv` и `os.getenv`, поэтому ошибка на `PyYAML 5.4.1` больше не должна появляться.[web:440][web:476]
