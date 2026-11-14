# Petit Studio Voix

Application web Django locale en français pour transformer des images JPG/PNG ou des pages PDF en vidéos MP4 narrées. Les projets, scènes, textes, réglages, échantillons vocaux et exports restent dans `media/` et SQLite (`db.sqlite3`). Aucun compte ni API cloud n’est requis ; une connexion est utile à l’installation et au premier téléchargement des poids IA, ensuite conservés localement.

Les projets, échantillons vocaux, narrations et sous-titres sont stockés dans `media/`; les données sont dans `db.sqlite3`. Aucun compte ni service cloud n’est nécessaire à l’usage. Une connexion Internet est nécessaire à l’installation des paquets et au premier téléchargement des poids IA, ensuite mis en cache localement. Le code ne désactive pas la vérification TLS.

## Fonctionnalités clés

- **Bibliothèque de voix** : import ou enregistrement d’échantillons WAV/MP3, écoute avant sauvegarde, normalisation locale en WAV mono 24 kHz et consentement explicite avant l’utilisation d’une voix clonée.
- **Projets nommés** : création, renommage et suppression confirmée des projets ; scènes modifiables et réordonnables par glisser-déposer.
- **Images et PDF** : import d’images JPG/PNG ou de documents PDF, chaque page du PDF devenant une scène image. Les mouvements et effets visuels peuvent être réglés scène par scène.
- **Narration vidéo** : import de clips HeyGen ou d’autres vidéos compatibles (MP4, MOV, M4V, WebM). Le mode Narration vidéo remplace leur audio d’origine par la voix XTTS et ajoute les sous-titres synchronisés. Si la voix dépasse la durée du clip, la dernière image est prolongée ; si le clip est plus long, il est coupé à la fin de la voix.
- **Assemblage classique** : assemblage de clips vidéo en conservant leur son d’origine, sans génération XTTS.
- **Synthèse vocale et alignement** : XTTS v2 génère la narration en français ; WhisperX aligne les sous-titres sur l’audio réellement produit.
- **Sous-titres style TikTok** : incrustation ASS synchronisée mot par mot, avec le mot prononcé surligné en orange, un contour noir et au maximum deux lignes.
- **Animations sur les scènes image** : combinaison de plusieurs effets (zoom, panoramique, parallaxe 2.5D, caméra à main levée, respiration lumineuse et fuite de lumière), avec intensités lente, moyenne ou rapide et profils de caméra linéaire ou organique.
- **Rendu et exports** : vidéo MP4 verticale 9:16 ou horizontale 16:9, aperçus et téléchargements de la vidéo, de l’audio WAV et des sous-titres SRT. Le rendu des images applique aussi un léger color grading.
- **File d’attente supervisable** : traitement séquentiel, suivi de la position et de la progression, segment en cours, temps écoulé, annulation d’un traitement et page de supervision.
- **Pré-génération audio** : pour les projets de narration vidéo, prépare XTTS et WhisperX sans lancer FFmpeg. Le badge de l’éditeur indique si l’audio de tous les segments est en cache ; les résultats sont réutilisés tant que le texte et la voix restent identiques.
- **Cache local** : les modèles IA et les audios préparés sont conservés localement. Le worker garde XTTS et WhisperX chargés pendant son exécution ; plusieurs projets peuvent être mis en file les uns à la suite des autres.
- **Interface locale responsive** : interface en français, sans dépendance à une police ou à une ressource distante.

## Prérequis

- Python 3.11 recommandé.
- FFmpeg et ffprobe pour les exports MP4, l’assemblage de clips et la normalisation audio. Sur macOS : `brew install ffmpeg`. L’incrustation des sous-titres ASS requiert le filtre FFmpeg `subtitles` compilé avec libass ; aucun paquet Python ASS supplémentaire n’est nécessaire. Si la version Homebrew installée ne fournit pas ce filtre, crée une fois l’environnement Conda dédié : `conda create -n ffmpeg-subs -c conda-forge ffmpeg`. L’application détecte automatiquement cet environnement aux emplacements Conda courants, sans commande `export` à répéter.
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

L’alias local `studio`, configuré dans `~/.zshrc`, démarre le site et le worker dans un seul terminal. Active d’abord l’environnement virtuel dans le dossier du dépôt :

```bash
source .venv/bin/activate
studio
```

Ouvre ensuite <http://127.0.0.1:8000/>. L’alias lance `python manage.py run_studio), qui démarre le site web et le worker XTTS/FFmpeg ensemble ; Ctrl+C arrête les deux. Si l’alias n’est pas configuré dans ton terminal, lance directement `python manage.py run_studio`. Le serveur de développement démarre sans rechargement automatique : après une modification du code, relance la commande.

Tu peux aussi lancer le site et le worker séparément dans deux terminaux :

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

1. Crée un projet. Pour une vidéo à partir d’images, importe des images JPG/PNG ou un PDF. Pour une narration vidéo, importe des clips MP4, MOV, M4V ou WebM (200 Mo maximum chacun) dans un projet dédié.
2. Réordonne les scènes ou les clips par glisser-déposer. Écris le texte à prononcer sur chaque scène ou clip ; sur les scènes image, choisis et combine les effets d’animation souhaités.
3. Ajoute une voix WAV/MP3 ou enregistre-la au microphone. Écoute-la, nomme-la, classe-la et confirme l’autorisation d’utilisation.
4. Pour les images, choisis le format 9:16 ou 16:9 et lance **Générer la vidéo MP4**. Pour les clips, choisis **Narration vidéo** pour remplacer le son d’origine par la voix XTTS et incruster les sous-titres, ou **Assembler les clips vidéo** pour conserver leur son d’origine.
5. Pour les clips narrés, **Pré-générer l’audio seul** lance XTTS et WhisperX sans FFmpeg. Quand le badge indique **Audio en cache**, lance le rendu vidéo ; tant que texte et voix restent identiques, les audios préparés sont réutilisés.
6. Suis la progression depuis le projet ou la page **File d’attente**. Prévisualise ou télécharge le MP4, l’audio WAV et les sous-titres SRT.

Si WhisperX échoue à aligner un segment, le projet passe en erreur avec le détail retourné ; aucun horodatage approximatif n’est créé.

## Licence et usage des voix

XTTS v2 est soumis à sa licence de modèle Coqui. Vérifie ses conditions avant un usage commercial. Enregistre uniquement ta voix ou une voix dont tu as obtenu l’autorisation explicite. L’application enregistre la confirmation de l’utilisateur mais ne peut pas vérifier les droits à sa place.
