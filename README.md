# Notes de frais — Association

Application web pour gérer les notes de frais d'une association : les
adhérents soumettent une note avec la photo du justificatif, le montant et
la date sont extraits automatiquement par OCR, et le trésorier valide,
refuse ou marque comme remboursée.

## Installation

Le scan des photos utilise Tesseract OCR, qui doit être installé sur le
serveur (en plus des paquets Python) :

```bash
# Debian / Ubuntu
sudo apt-get install tesseract-ocr tesseract-ocr-fra

# macOS (Homebrew)
brew install tesseract tesseract-lang
```

Puis installer les dépendances Python :

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

> Remarque : si `tesseract-ocr-fra` n'est pas installé, l'OCR utilise le
> modèle anglais par défaut. Les montants et dates (chiffres) sont en général
> bien détectés quand même ; pour une meilleure reconnaissance des mots
> (nom du commerçant, etc.), installez le pack de langue française et
> changez `lang="eng"` en `lang="fra"` dans `app.py` (fonction `run_ocr`).

## Lancement

```bash
python app.py
```

Puis ouvrir http://127.0.0.1:5000 (ou l'adresse du serveur en production).

**Le tout premier compte créé devient automatiquement le compte trésorier.**
Les inscriptions suivantes sont des comptes membres. Le trésorier peut
ensuite promouvoir ou rétrograder d'autres membres depuis la page "Membres".

## Fonctionnement

1. **Un membre** se connecte, va dans "+ Nouvelle note", prend en photo son
   ticket ou sa facture.
2. L'appli lance l'OCR sur la photo et propose un montant et une date ;
   le membre vérifie/corrige, choisit une catégorie, ajoute une description
   optionnelle, puis envoie la note.
3. **Le trésorier** voit toutes les notes "en attente" sur son tableau de
   bord, ouvre chaque note pour voir la photo du justificatif, et la
   valide, la refuse (avec un commentaire) ou la marque "remboursée" une
   fois le virement fait.
4. Le trésorier peut exporter l'ensemble des notes en CSV (séparateur `;`,
   compatible Excel) pour la comptabilité de l'association.

## Déploiement sur Railway

Le projet contient déjà les fichiers nécessaires (`Procfile`, `nixpacks.toml`,
`gunicorn` dans `requirements.txt`).

1. Mettre le dossier dans un dépôt GitHub (Railway se connecte à un repo).
2. Sur [railway.app](https://railway.app), créer un nouveau projet →
   "Deploy from GitHub repo" → sélectionner le dépôt.
3. Railway détecte `nixpacks.toml` et installe automatiquement
   `tesseract-ocr` + `tesseract-ocr-fra` en plus des paquets Python.
4. Dans l'onglet **Variables** du service, ajouter :
   - `SECRET_KEY` : une valeur aléatoire longue (obligatoire, ne pas garder
     la valeur par défaut du code).
5. **Important — stockage persistant** : par défaut, le système de fichiers
   de Railway est effacé à chaque redéploiement. Or la base SQLite
   (`notes_de_frais.db`) et les photos de justificatifs (`uploads/`) sont
   stockées sur disque. Pour ne pas tout perdre au prochain déploiement :
   - Dans l'onglet **Volumes** du service, créer un volume et le monter sur
     `/app` (ou au minimum sur `/app/uploads`, mais garder aussi le fichier
     `.db` sur le volume).
   - Alternative plus robuste à terme : ajouter une base **PostgreSQL**
     (plugin Railway) plutôt que SQLite — demande une adaptation du code
     de `app.py`, à faire si l'association grandit ou si plusieurs
     instances tournent en parallèle.
6. Railway génère une URL publique (`https://....up.railway.app`) —
   c'est le lien à partager avec les membres et le trésorier.

## Déploiement en production (autre hébergeur)

Pour un vrai déploiement (accessible en ligne, pas seulement en local) :

- Utiliser un serveur WSGI comme **gunicorn** derrière **nginx**, plutôt que
  le serveur de développement Flask (`app.run(debug=True)`).
- Changer `SECRET_KEY` dans `app.py` (ou la définir via une variable
  d'environnement `SECRET_KEY`) — ne jamais garder la valeur par défaut.
- Mettre le fichier `notes_de_frais.db` (SQLite) et le dossier `uploads/`
  dans un emplacement sauvegardé régulièrement (les justificatifs photo y
  sont stockés).
- Servir le site en HTTPS, en particulier parce que les mots de passe et
  les justificatifs (souvent avec des informations personnelles) y
  transitent.

## Limites connues / pistes d'amélioration

- L'OCR est une aide au remplissage, pas une garantie : le montant/la date
  détectés doivent toujours être vérifiés par l'utilisateur avant envoi.
- Pas de récupération de mot de passe par email pour l'instant (à ajouter
  si besoin, avec un serveur SMTP).
- Un seul niveau de "trésorier" ; si l'association a besoin de plusieurs
  rôles (ex. trésorier adjoint, président en lecture seule), le modèle
  `users.role` peut être étendu facilement.
