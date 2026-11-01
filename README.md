# Petit Studio Voix

Application web Django locale en français pour transformer des images JPG/PNG ou des pages PDF en vidéos MP4 narrées. Les projets, scènes, textes, réglages, échantillons vocaux et exports restent dans `media/` et SQLite (`db.sqlite3`). Aucun compte ni API cloud n’est requis ; une connexion est utile à l’installation et au premier téléchargement des poids IA, ensuite conservés localement.

Les projets, échantillons vocaux, narrations et sous-titres sont stockés dans `media/`; les données sont dans `db.sqlite3`. Aucun compte ni service cloud n’est nécessaire à l’usage. Une connexion Internet est nécessaire à l’installation des paquets et au premier téléchargement des poids IA, ensuite mis en cache localement. Le code ne désactive pas la vérification TLS.

## Fonctionnalités

- Bibliothèque locale avec catégories Shiba, Homme, Femme et Autre, recherche et filtre.
- Import WAV/MP3 ou enregistrement microphone, écoute avant sauvegarde, normalisation locale en WAV mono 24 kHz.
- Consentement explicite avant l’enregistrement d’une voix clonée.
- Renommage, recatégorisation et suppression confirmée des voix.
- Projets avec plusieurs segments de texte modifiables, supprimables et réordonnables.
- Import local d’images JPG/PNG ou de PDF ; chaque page PDF est convertie en une scène image.
- Import de clips MP4, MOV, M4V ou WebM dans un projet dédié ; réorganisation des clips et assemblage MP4 FFmpeg avec son d’origine conservé, sans synthèse IA.
- Aperçu des images sélectionnées avant de confirmer leur import dans le projet.
- Aperçu des scènes, réorganisation par glisser-déposer et mouvements de caméra par scène : image fixe, zoom avant/arrière, panoramique horizontal/vertical, zoom diagonal ou parallaxe 2.5D « profondeur douce » avec sujet détouré localement.
- Synthèse française XTTS v2 et alignement forcé WhisperX sur l’audio réellement généré.
- Sous-titres synchronisés avec l’audio ; les phrases sont incrustées progressivement dans la vidéo, avec activation/désactivation.
- Rendu MP4 FFmpeg en 9:16 ou 16:9, proportions conservées, prévisualisation et téléchargement.
- La parallaxe crée une impression de profondeur sans animer le visage ni les gestes du sujet.
- File d’attente persistante des projets : un worker séquentiel garde XTTS et WhisperX chargés en mémoire pendant qu’il traite plusieurs narrations.
- Cache disque par texte, empreinte SHA-256 de l’échantillon, modèle et langue ; les segments inchangés sont réutilisés.
- Écoute des segments, plus export WAV/SRT et vidéo MP4.
- Progression persistante en base, position dans la file, segment courant, temps écoulé, durées XTTS/WhisperX et état prêt/en attente/en cours/erreur.
- Interface responsive inspirée des captures fournies, sans dépendance à une police ou ressource distante.

## Prérequis

- Python 3.11 recommandé.
- FFmpeg et ffprobe pour les exports MP4, l’assemblage de clips et la normalisation audio. Sur macOS : `brew install ffmpeg`. L’incrustation des sous-titres requiert aussi le filtre `subtitles` (libass). Si la version Homebrew installée ne le fournit pas, crée une fois l’environnement Conda dédié : `conda create -n ffmpeg-subs -c conda-forge ffmpeg`. L’application détecte automatiquement cet environnement aux emplacements Conda courants, sans commande `export` à répéter.
- Pillow et PyMuPDF sont installés par `requirements.txt` pour valider les images et convertir les PDF.
- Le mouvement « Profondeur douce » est facultatif et utilise rembg/ONNX en local : installe `requirements-parallax.txt`. Ses versions sont fixées pour rester compatibles avec XTTS (NumPy 1.26.4, NetworkX 2.8.8) et les Mac Intel. Le modèle U2NetP est téléchargé au premier rendu et réutilisé ensuite localement.
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
# Facultatif, pour activer le mouvement « Profondeur douce »
python -m pip install -r requirements-parallax.txt
python manage.py migrate
python scripts/check_ai_install.py
```

Le jeu de versions IA est volontairement fixé dans `requirements-ai.txt` pour Python 3.11 et les environnements Mac Intel qui nécessitent PyTorch 2.2.2. Installe-le dans un environnement virtuel neuf et vérifie que le terminal affiche bien `(.venv)` avant chaque commande. Si tu as déjà le projet, relance `python -m pip install -r requirements.txt` pour installer Pillow et PyMuPDF. Si `pip` tente de compiler `llvmlite`, arrête l’installation et vérifie l’architecture et la version de Python utilisées ; ne lance pas de compilation LLVM manuelle.

Sur CPU, l’application limite PyTorch à quatre fils de calcul au maximum (ou au nombre de cœurs disponibles). Pour comparer les performances sur ta machine, définis `STUDIO_TORCH_THREADS` avant le démarrage, par exemple `export STUDIO_TORCH_THREADS=2`, puis relance l’application. Essaie 1, 2 et 4 : le meilleur réglage dépend du processeur. Le modèle XTTS, l’alignement WhisperX et les latents vocaux sont conservés en mémoire par le worker pendant son exécution. Les durées mesurées de synthèse et d’alignement sont affichées à la fin de la préparation.

Le script vérifie l’import de PyTorch et XTTS, `Xtts.inference` et la méthode autoregressive `GPT.generate` appelée pendant la synthèse. Pour vérifier aussi une synthèse complète, donne-lui un échantillon vocal autorisé :

```bash
python scripts/check_ai_install.py --sample /chemin/vers/mon-echantillon.wav
```

Cette commande charge XTTS v2 et télécharge ses poids si nécessaire. Elle écrit le WAV de test dans un dossier temporaire qui est supprimé à la fin.

## Lancement

Pour démarrer le site et le worker depuis un seul terminal, active `.venv` dans le dossier du dépôt puis exécute :

```bash
python manage.py run_studio
```

Ouvre ensuite <http://127.0.0.1:8000/>. Cette commande démarre le site web et le worker XTTS/FFmpeg ensemble ; Ctrl+C arrête les deux. Le serveur de développement est lancé sans rechargement automatique : après une modification du code, relance la commande.

Tu peux aussi les lancer séparément dans deux terminaux si tu préfères :

```bash
python manage.py runserver 127.0.0.1:8000
# Dans l’autre terminal, avec le même environnement virtuel :
python manage.py run_narration_worker
```

Ajoute des projets à la file depuis leurs pages ; le worker les traite dans l’ordre, un par un. La file est enregistrée dans SQLite et reprend les traitements interrompus après un redémarrage. Le serveur de développement est prévu pour l’accès local ; ne l’expose pas directement sur Internet.

## Tests et contrôles

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

Les tests Django couvrent l’autorisation et la gestion des voix, les imports image/PDF, l’ordre des scènes et de la file, le rendu vidéo, les clés de cache, l’alignement sans horodatages de secours, l’assemblage WAV et les offsets SRT. Le test d’import XTTS est automatiquement ignoré si les dépendances IA ne sont pas installées ; le script `check_ai_install.py` fournit le contrôle dédié.

## Utilisation

1. Crée un projet. Pour une vidéo narrée, importe des images ou un PDF. Pour réunir des vidéos, importe uniquement des clips MP4, MOV, M4V ou WebM (200 Mo maximum chacun) dans un projet séparé.
2. Réordonne les scènes par glisser-déposer ; ajoute le texte et choisis une image fixe ou un zoom pour chacune.
3. Ajoute une voix WAV/MP3 ou enregistre-la au microphone. Écoute-la, nomme-la, classe-la et confirme l’autorisation d’utilisation.
4. Pour les images, choisis les sous-titres et le format 9:16 ou 16:9, puis clique sur **Générer la vidéo MP4**. Pour des clips, réordonne-les, choisis le format et clique sur **Assembler les clips vidéo** ; le son d’origine est conservé.
5. La file traite les projets dans l’ordre ; la page affiche la position et l’avancement. Les audios dont le texte et la voix n’ont pas changé sont réutilisés depuis le cache. Prévisualise ou télécharge le MP4, le WAV et le SRT.

Si WhisperX échoue à aligner un segment, le projet passe en erreur avec le détail retourné ; aucun horodatage approximatif n’est créé.

## Licence et usage des voix

XTTS v2 est soumis à sa licence de modèle Coqui. Vérifie ses conditions avant un usage commercial. Enregistre uniquement ta voix ou une voix dont tu as obtenu l’autorisation explicite. L’application enregistre la confirmation de l’utilisateur mais ne peut pas vérifier les droits à sa place.
