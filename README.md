# Petit Studio Voix

Application web Django locale en français pour gérer une bibliothèque de voix, écrire des narrations en segments, les synthétiser avec XTTS v2, écouter chaque segment, puis exporter un WAV complet et son fichier SRT synchronisé.

Les projets, échantillons vocaux, narrations et sous-titres sont stockés dans `media/`; les données sont dans `db.sqlite3`. Aucun compte ni service cloud n’est nécessaire à l’usage. Une connexion Internet est nécessaire à l’installation des paquets et au premier téléchargement des poids IA, ensuite mis en cache localement. Le code ne désactive pas la vérification TLS.

## Fonctionnalités

- Bibliothèque locale avec catégories Shiba, Homme, Femme et Autre, recherche et filtre.
- Import WAV/MP3 ou enregistrement microphone, écoute avant sauvegarde, normalisation locale en WAV mono 24 kHz.
- Consentement explicite avant l’enregistrement d’une voix clonée.
- Renommage, recatégorisation et suppression confirmée des voix.
- Projets avec plusieurs segments de texte modifiables, supprimables et réordonnables.
- Synthèse française XTTS v2 et alignement forcé WhisperX sur l’audio réellement généré.
- Réutilisation en mémoire du modèle XTTS, du modèle d’alignement WhisperX et des représentations de chaque voix pendant l’exécution du serveur.
- Cache disque par texte, empreinte SHA-256 de l’échantillon, modèle et langue ; les segments inchangés sont réutilisés.
- Écoute et téléchargement de chaque segment, plus export WAV complet et SRT avec chronologie cumulée.
- Progression persistante en base, segment courant, temps écoulé, durées XTTS/WhisperX et état prêt/en cours/erreur.
- Interface responsive inspirée des captures fournies, sans dépendance à une police ou ressource distante.

## Prérequis

- Python 3.11 recommandé.
- FFmpeg dans le PATH pour normaliser les échantillons WAV/MP3/WebM. Sur macOS : `brew install ffmpeg`.
- Environ 8 à 10 Go libres pour les poids XTTS/WhisperX téléchargés au premier lancement. La synthèse CPU peut être lente ; une carte NVIDIA/CUDA accélère le traitement sur les systèmes compatibles.

## Installation sur macOS

Ouvre Terminal dans le dossier du dépôt :

```bash
brew install ffmpeg
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -r requirements-ai.txt
python manage.py migrate
python scripts/check_ai_install.py
```

Le jeu de versions IA est volontairement fixé dans `requirements-ai.txt` pour Python 3.11 et les environnements Mac Intel qui nécessitent PyTorch 2.2.2. Installe-le dans un environnement virtuel neuf et vérifie que le terminal affiche bien `(.venv)` avant chaque commande. Si `pip` tente de compiler `llvmlite`, arrête l’installation et vérifie l’architecture et la version de Python utilisées ; ne lance pas de compilation LLVM manuelle.

Sur CPU, l’application limite PyTorch à quatre fils de calcul au maximum (ou au nombre de cœurs disponibles). Pour comparer les performances sur ta machine, définis `STUDIO_TORCH_THREADS` avant le démarrage, par exemple `export STUDIO_TORCH_THREADS=2`, puis relance le serveur. Essaie 1, 2 et 4 : le meilleur réglage dépend du processeur. Le modèle XTTS, l’alignement WhisperX et les latents vocaux sont conservés en mémoire tant que le processus Django reste actif. Les durées mesurées de synthèse et d’alignement sont affichées à la fin de la préparation.

Le script vérifie l’import de PyTorch et XTTS, `Xtts.inference` et la méthode autoregressive `GPT.generate` appelée pendant la synthèse. Pour vérifier aussi une synthèse complète, donne-lui un échantillon vocal autorisé :

```bash
python scripts/check_ai_install.py --sample /chemin/vers/mon-echantillon.wav
```

Cette commande charge XTTS v2 et télécharge ses poids si nécessaire. Elle écrit le WAV de test dans un dossier temporaire qui est supprimé à la fin.

## Lancement

Dans le même environnement virtuel :

```bash
python manage.py runserver 127.0.0.1:8000
```

Ouvre ensuite <http://127.0.0.1:8000/>. Le serveur de développement est prévu pour l’accès local ; ne l’expose pas directement sur Internet.

## Tests et contrôles

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

Les tests Django couvrent l’autorisation et la gestion des voix, les segments et leur réorganisation, les clés de cache, l’alignement sans horodatages de secours, l’assemblage WAV et le calcul des offsets SRT. Le test d’import XTTS est automatiquement ignoré si les dépendances IA ne sont pas installées ; le script `check_ai_install.py` fournit le contrôle dédié.

## Utilisation

1. Ajoute une voix WAV/MP3 ou enregistre-la avec le microphone ; écoute-la avant de la sauvegarder et confirme les droits d’utilisation.
2. Crée un projet de narration, écris tes textes en segments et choisis une voix enregistrée.
3. Clique sur **Préparer les segments**. XTTS crée les audios et WhisperX aligne chaque texte sur son audio. Les segments déjà prêts et inchangés sont réutilisés.
4. Écoute ou télécharge chaque WAV/SRT séparément, puis récupère la narration complète et son SRT.

Si WhisperX échoue à aligner un segment, le projet passe en erreur avec le détail retourné ; aucun horodatage approximatif n’est créé.

## Licence et usage des voix

XTTS v2 est soumis à sa licence de modèle Coqui. Vérifie ses conditions avant un usage commercial. Enregistre uniquement ta voix ou une voix dont tu as obtenu l’autorisation explicite. L’application enregistre la confirmation de l’utilisateur mais ne peut pas vérifier les droits à sa place.
