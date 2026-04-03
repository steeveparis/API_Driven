# API-Driven Infrastructure

Piloter une instance EC2 (start/stop/status) via des requêtes HTTP en passant par API Gateway + Lambda.  
Tout tourne en local grâce à **LocalStack** dans un **GitHub Codespace**.

```
HTTP GET → API Gateway (/ec2) → Lambda (Python) → EC2
```

---

## Prérequis

- GitHub Codespace
- Token LocalStack → [app.localstack.cloud](https://app.localstack.cloud)

## Structure

```
├── lambda/handler.py       # Lambda qui pilote l'EC2
├── scripts/setup.sh        # Déploiement auto de toute l'infra
├── Makefile                 # Commandes make
└── .gitignore
```

---

## Lancer le projet

```bash
# 1. Premier lancement (installe tout, demande le token)
make first-setup

# 2. Déployer l'infra
make deploy
```

Le deploy crée dans l'ordre : AMI → EC2 → rôle IAM → Lambda → API Gateway.  
Les 3 URLs s'affichent à la fin.

## Tester

```bash
curl "<URL_START>"    # Démarre l'instance
curl "<URL_STOP>"     # Stoppe l'instance
curl "<URL_STATUS>"   # Vérifie l'état
```

Réponse type :
```json
{"message": "Instance i-xxxx is running"}
```

Pour tester dans le navigateur : rendre le port **4566 public** dans l'onglet PORTS du Codespace, puis coller l'URL directement.

---

## Autres commandes

| Commande | Rôle |
|----------|------|
| `make setup` | Relancer LocalStack (redemande le token) |
| `make clean` | Stopper LocalStack + supprimer les fichiers temp |
| `make redeploy` | Clean + relance tout depuis zéro |

---

## Problèmes courants

**`awslocal` plante avec `No such file or directory: aws`**  
→ `pip install awscli --break-system-packages`

**Lambda en état `Pending` / `ResourceConflictException`**  
→ Attendre quelques secondes, la Lambda s'initialise.

**Lambda retourne `Could not connect to endpoint`**  
→ Le handler.py doit utiliser l'IP Docker (`172.17.0.2`) et non `localhost`.

**`InvalidAMI.ID.NotFound`**  
→ Le setup.sh enregistre un AMI automatiquement via `register-image`. Ne pas utiliser un ID fictif.

**`Could not connect to localhost:4566`**  
→ LocalStack ne tourne plus. Relancer avec `make setup`.
