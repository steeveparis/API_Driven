#!/usr/bin/env bash
set -e

echo "[1/5] Création environnement Python"
mkdir -p rep_localstack
python3 -m venv rep_localstack

echo "[2/5] Activation environnement virtuel"
source rep_localstack/bin/activate

echo "[3/5] Installation LocalStack + AWS CLI"
pip install --upgrade pip
pip install localstack awscli boto3

echo "[4/5] Lancement LocalStack"
export S3_SKIP_SIGNATURE_VALIDATION=0
localstack start -d

echo "[5/5] Vérification services"
sleep 10
localstack status services
