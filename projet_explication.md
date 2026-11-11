# Documentation détaillée du projet Petit Studio Voix

## 1. Vue d'ensemble

Petit Studio Voix est une application web Django locale qui transforme du texte et des images en narrations audio et en vidéos MP4. Elle permet aussi d'assembler des clips vidéo existants en conservant leur son. L'interface est en français et les traitements sont conçus pour s'exécuter sur l'ordinateur de l'utilisateur, sans compte ni API cloud.

L'application couvre deux parcours vidéo distincts :

1. **Créer une vidéo narrée** : importer une image JPG/PNG ou un PDF, écrire un texte pour chaque scène, choisir une voix, générer la narration, aligner les sous-titres sur l'audio, puis créer une vidéo avec un mouvement d'image facultatif.
2. **Assembler des clips existants** : importer seulement des clips vidéo dans un projet, les réordonner, puis les normaliser et les concaténer avec leur son d'origine. Ce parcours ne lance pas XTTS.

Un projet peut également produire uniquement une narration audio, avec un export WAV complet et un fichier SRT. Les médias et les exports résident dans le dossier `MEDIA_ROOT` (par défaut `media/`) ; les métadonnées et l'état de la file sont stockés dans SQLite (`db.sqlite3`).

## 2. Stack technique

### Application et interface

- **Python** : le README recommande Python 3.11, en particulier pour la pile IA. Le workflow CI utilise Python 3.12 et n'installe que les dépendances de base.
- **Django** : `Django>=5.2,<5.3` dans `requirements.txt` ; la version exacte n'est pas verrouillée.
- **Base de données** : SQLite, fichier `db.sqlite3` à la racine du projet.
- **Interface** : templates Django, CSS et JavaScript natif ; aucun framework JavaScript n'est utilisé.
- **Traitement des images et PDF** : Pillow et PyMuPDF (`pymupdf`).

### Audio, IA et vidéo

Les dépendances IA sont isolées dans `requirements-ai.txt` :

- **Coqui TTS / XTTS v2** (`coqui-tts==0.22.1`) pour la synthèse vocale conditionnée par un échantillon de voix.
- **PyTorch et torchaudio 2.2.2** pour l'inférence locale, avec CUDA utilisée si elle est disponible.
- **WhisperX 3.3.1** pour l'alignement des mots du texte avec l'audio réellement produit.
- **Transformers 4.49.0**, NumPy 1.26.4, Numba 0.62.1 et llvmlite 0.45.1, fixés pour la compatibilité de cette pile.

Les fichiers audio et vidéo sont traités avec **FFmpeg** et **ffprobe**, qui doivent être installés séparément du paquet Python. L'incrustation des sous-titres demande un FFmpeg avec le filtre `subtitles`/libass.

La parallaxe est facultative et définie dans `requirements-parallax.txt` : rembg 2.0.67 avec le modèle U2NetP, ONNX Runtime 1.16.3, OpenCV headless, scikit-image, NetworkX 2.8.8 et NumPy 1.26.4. NetworkX n'est pas importé directement par le code applicatif ; il est inclus dans cet ensemble de dépendances.

## 3. Architecture du projet

Le projet Django comporte une seule application métier, `studio`. `config` est le paquet de configuration Django, pas une application fonctionnelle séparée.

```text
config/                         Réglages et points d'entrée Django
  settings.py                   Paramètres, SQLite, médias, middleware
  urls.py                       Admin Django et inclusion des routes studio
  asgi.py / wsgi.py             Points d'entrée ASGI et WSGI
studio/                         Application de création audio/vidéo
  models.py                     Voix, projets et segments
  views.py                      Parcours HTTP et validation des formulaires/imports
  services.py                   XTTS, WhisperX, cache audio, WAV et SRT
  video_services.py             Rendu MP4, mouvements, parallaxe et assemblage de clips
  urls.py                       Routes de l'application
  admin.py                      Configuration de l'administration Django
  templates/studio/             Pages HTML de la bibliothèque et des projets
  static/studio/                CSS et JavaScript du navigateur
  management/commands/          Worker de file et commande de lancement combiné
  migrations/                   Évolution du schéma de données
scripts/                        Vérifications hors interface Django
requirements*.txt               Dépendances de base, IA et parallaxe
.github/workflows/              CI et workflow manuel de rollback
```

### Rôle des principaux modules

- **`config/settings.py`** configure Django, les middleware, les templates, SQLite, les fichiers statiques et les médias. La configuration est volontairement orientée vers un usage local.
- **`config/urls.py`** expose l'administration Django sous `/admin/` et inclut toutes les routes `studio` à la racine.
- **`studio/views.py`** traite les requêtes du navigateur : bibliothèque des voix, création et édition des projets, import et validation des médias, changements de scènes, lancement des traitements et interrogation de leur état.
- **`studio/services.py`** contient la chaîne audio : conversion d'échantillon, chargement XTTS, génération des segments, alignement WhisperX, cache, assemblage WAV et écriture SRT.
- **`studio/video_services.py`** contient la chaîne vidéo FFmpeg : filtres de mouvement, sous-titres, parallaxe 2.5D et assemblage de clips.
- **`studio/management/commands/run_narration_worker.py`** consomme la file persistante, un projet après l'autre.
- **`studio/management/commands/run_studio.py`** lance le serveur Django et le worker dans le même terminal ; `Ctrl+C` arrête les deux.
- **`scripts/check_ai_install.py`** vérifie les imports et l'API XTTS. Avec `--sample`, il exécute aussi une courte synthèse réelle.
- **`studio/static/studio/app.js`** gère les aperçus locaux, l'enregistrement micro, le glisser-déposer des scènes, l'enregistrement des changements non soumis et le suivi périodique de la file.

## 4. Modèles de données

Le schéma est défini dans `studio/models.py` et suivi par les migrations de `studio/migrations/`.

### `Voice`

Représente une voix enregistrée dans la bibliothèque.

- `name`, `category` (Shiba, homme, femme ou autre).
- `sample` : fichier audio stocké sous `MEDIA_ROOT/voices/`.
- `sample_sha256` : empreinte utilisée notamment pour identifier le cache.
- `consent_confirmed` : confirmation explicite que l'utilisateur est autorisé à cloner et utiliser cette voix.
- Dates de création et de mise à jour.

Une voix peut être référencée par plusieurs projets. La relation depuis `NarrationProject` est nullable et utilise `SET_NULL` : supprimer une voix ne supprime donc pas les projets qui la référencent.

### `NarrationProject`

Représente un projet audio ou vidéo et porte l'état du traitement.

- `title`, voix sélectionnée, `job_type` et `video_format`.
- États : `draft`, `queued`, `processing`, `ready`, `error`.
- Types de tâche : `audio`, `video` et `assembly`.
- Réglage `subtitles_enabled`, messages d'état et d'erreur, progression et segment courant/total.
- Horodatages de mise en file, début, fin, création et modification.
- Fichiers d'export : WAV complet, SRT et MP4 ; durée totale.

Un projet possède plusieurs `NarrationSegment`. La suppression d'un projet supprime ses segments en base (`CASCADE`) ; les vues suppriment aussi les fichiers associés.

### `NarrationSegment`

Représente une scène ou un clip ordonné dans un projet.

- `project` : clé étrangère vers le projet ; `position` définit l'ordre.
- `image` pour une scène illustrée ou `video_clip` pour un clip à assembler. Un projet est destiné à l'un de ces types de média à la fois.
- `text`, `motion`, l'audio WAV généré, `subtitle_data` (JSON), durée et `cache_key`.
- Les choix de mouvement incluent l'image fixe, les zooms, les panoramiques, le zoom diagonal et la parallaxe.

## 5. Fonctionnalités clés

### 5.1 Bibliothèque locale de voix

Les routes sont définies dans `studio/urls.py` et les vues correspondantes dans `studio/views.py` (`home`, `create_voice`, `edit_voice`, `delete_voice`). La page d'accueil recherche et filtre les voix, et affiche les projets récents.

À l'enregistrement, la vue valide le nom, la catégorie, la confirmation de consentement, l'extension et la taille (25 Mo maximum). `normalize_voice_sample()` dans `studio/services.py` appelle FFmpeg pour convertir l'échantillon en WAV mono PCM à 24 kHz ; l'empreinte SHA-256 est ensuite enregistrée. Le navigateur peut aussi enregistrer un échantillon via `MediaRecorder` dans `app.js`.

### 5.2 Projets, scènes et import de médias

`create_project` crée un projet et son premier segment vide. `project_detail` prépare les données de la page d'édition. Les opérations de création, modification, suppression, réordonnancement et import sont des vues dédiées dans `studio/views.py` et des routes dans `studio/urls.py`.

- Images : JPG, JPEG et PNG, maximum 25 Mo par fichier et 40 millions de pixels.
- PDF : maximum 25 Mo, 30 pages par import ; chaque page est convertie en image PNG. Le rendu applique aussi des limites de pixels par page et par import.
- Clips : MP4, MOV, M4V ou WebM, maximum 200 Mo par clip ; maximum 30 scènes/clips par import.
- Les images/PDF et les clips vidéo ne peuvent pas être mélangés dans un même projet.

Le navigateur présente un aperçu avant import. Le glisser-déposer réordonne les segments puis envoie leurs identifiants à `reorder_segments`, qui vérifie que la liste contient exactement les segments du projet. Les mutations sont refusées pendant les états `queued` et `processing`.

### 5.3 Narration XTTS, cache et sous-titres synchronisés

Le formulaire de préparation appelle `prepare` dans `studio/views.py`. Cette vue vérifie la voix autorisée, la présence d'un texte pour chaque segment et, pour le rendu vidéo narré, une image par scène. Elle enregistre ensuite le travail comme tâche `audio` ou `video` dans la file SQLite.

Le worker appelle `prepare_project()` dans `studio/services.py`. Pour chaque segment :

1. Il calcule une clé de cache à partir du texte, de l'empreinte de l'échantillon vocal, du modèle et de la langue.
2. Si un résultat correspondant existe, il réutilise l'audio et les sous-titres.
3. Sinon, XTTS génère le segment. Le modèle et les latents de voix sont gardés en mémoire pendant la durée de vie du worker ; un verrou évite les inférences XTTS concurrentes.
4. FFmpeg normalise l'audio en WAV mono 24 kHz.
5. WhisperX aligne le texte fourni sur l'audio généré. Les mots sont groupés pour former des sous-titres lisibles ; si aucun mot ne peut être aligné, le segment échoue au lieu d'inventer des minutages.

À la fin, `_assemble_wav()` concatène les WAV compatibles sans réencodage, et `srt_text()` décale les minutages de chaque segment dans la chronologie globale. Les exports sont enregistrés dans `MEDIA_ROOT/exports/`; le cache partagé est dans `MEDIA_ROOT/audio-cache/`.

### 5.4 Rendu vidéo et assemblage de clips

Pour un projet avec images, `render_project_video()` dans `studio/video_services.py` produit une vidéo pour chaque scène à 24 images/s, en 720×1280 (vertical) ou 1280×720 (horizontal). FFmpeg conserve le rapport d'image, applique le mouvement sélectionné, ajoute l'audio du segment et, si demandé, incruste le SRT synchronisé. Les clips de scène sont ensuite concaténés et enregistrés comme MP4.

La parallaxe est un chemin facultatif : rembg/U2NetP détoure un sujet ; le code superpose ensuite le sujet net à un fond flouté et assombri, en faisant dériver les deux couches. Ce procédé donne une impression de profondeur mais n'anime pas le visage ni le corps.

Pour l'assemblage de clips, `assemble_videos()` met une tâche `assembly` en file. `render_video_assembly()` utilise ffprobe pour trouver durée et piste audio, normalise chaque clip en H.264/AAC, 24 images/s et aux dimensions choisies ; une piste silencieuse est ajoutée si le clip n'a pas de son. Les clips normalisés sont ensuite concaténés, en conservant le son d'origine lorsqu'il existe.

## 6. Points d'attention

### Configuration et exploitation

Les variables d'environnement lues par le code sont :

| Variable | Rôle | Comportement si absente |
|---|---|---|
| `DJANGO_SECRET_KEY` | Clé secrète Django | Une clé aléatoire est générée au démarrage ; elle change au redémarrage. |
| `DJANGO_DEBUG` | Active `DEBUG` uniquement si sa valeur est exactement `1` | Vaut `1` par défaut, donc `DEBUG=True`. Utiliser `0` ou `False` pour désactiver le mode debug. |
| `MEDIA_ROOT` | Répertoire des médias, caches et verrou du worker | `media/` à la racine du dépôt. |
| `FFMPEG_BINARY` | Chemin explicite vers FFmpeg | Le code cherche d'abord les environnements Conda courants, puis `ffmpeg` dans le `PATH`. |
| `CONDA_PREFIX`, `CONDA_EXE` | Aident à détecter le FFmpeg Conda avec libass | Détection ignorée si ces variables ne sont pas définies ; des chemins macOS connus sont également essayés. |
| `STUDIO_TORCH_THREADS` | Nombre de fils CPU PyTorch | Au plus 4 ou le nombre de cœurs détectés. |

`ALLOWED_HOSTS` est actuellement défini en dur à `127.0.0.1` et `localhost`. La base est également définie en dur sur SQLite ; il n'y a pas de lecture de `DATABASE_URL` dans `settings.py`. L'interface n'a pas de comptes utilisateurs et le README précise de ne pas exposer le serveur de développement sur Internet.

Lancer `python manage.py migrate`, puis `python manage.py run_studio`. Cette commande démarre le serveur sur `127.0.0.1:8000` et le worker. Le worker utilise `fcntl` pour verrouiller `MEDIA_ROOT/.narration-worker.lock` et empêcher un second worker d'utiliser le même dossier média ; ce mécanisme est POSIX (macOS/Linux), pas Windows natif.

### Dépendances lourdes et premier démarrage

- Installer FFmpeg et ffprobe au niveau système ; vérifier la présence du filtre libass si les sous-titres doivent être incrustés.
- Les poids XTTS et les composants d'alignement WhisperX peuvent nécessiter environ 8 à 10 Go libres et sont téléchargés au premier usage. La génération CPU peut être lente.
- `requirements-ai.txt` est une matrice versionnée pour Python 3.11, notamment pour les Mac Intel. La CI Python 3.12 n'installe pas ce fichier : elle valide les tests non-IA et les contrôles Django, pas une inférence XTTS réelle.
- Installer `requirements-parallax.txt` uniquement si la parallaxe est utilisée ; rembg peut télécharger le modèle U2NetP au premier rendu.
- Vérifier la licence XTTS v2 de Coqui et les droits associés à chaque échantillon vocal avant un usage commercial. Le champ de consentement enregistre une déclaration ; l'application ne vérifie pas les droits elle-même.

### Limites et éléments à vérifier dans le code actuel

- Les imports vidéo peuvent atteindre 30 clips de 200 Mo chacun. La vue lit les fichiers dans une liste en mémoire avant de les sauvegarder ; de gros imports peuvent donc consommer beaucoup de RAM.
- Dans `render_video_assembly()`, la ligne qui construit `clips.txt` contient `\\n` dans la chaîne Python. Cela écrit vraisemblablement les caractères littéraux `\n` au lieu d'une nouvelle ligne entre les entrées ; le manifeste concaténé doit être vérifié avec FFmpeg. Le rendu d'images utilise, lui, un saut de ligne `\n` réel.
- Les tests de `studio/tests.py` couvrent notamment les vues voix/projets, la file, les imports image/PDF, des fonctions de rendu, le cache, l'alignement et le WAV. Le test XTTS ne s'exécute que si les dépendances IA sont présentes ; `scripts/check_ai_install.py --sample ...` sert à contrôler une synthèse réelle.

Pour vérifier le projet sans exécuter de synthèse IA :

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

Pour vérifier les dépendances IA et éventuellement générer un WAV de contrôle :

```bash
python scripts/check_ai_install.py
python scripts/check_ai_install.py --sample /chemin/vers/une-voix-autorisee.wav
```
