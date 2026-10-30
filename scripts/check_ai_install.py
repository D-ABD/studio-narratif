#!/usr/bin/env python3
"""Check XTTS imports and GPT.generate; optionally synthesize a real French smoke sample."""
import argparse
from pathlib import Path
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", help="WAV/MP3 voice sample; performs a real XTTS smoke synthesis")
    args = parser.parse_args()
    try:
        import torch
        from TTS.api import TTS
        from TTS.tts.models.xtts import Xtts
        from TTS.tts.layers.xtts.gpt import GPT
    except ImportError as exc:
        raise SystemExit(f"Dépendance XTTS manquante dans cet environnement : {exc}") from exc
    if not callable(getattr(Xtts, "inference", None)) or not callable(getattr(GPT, "generate", None)):
        raise SystemExit("Échec : Xtts.inference ou GPT.generate n’est pas callable dans cette version.")
    print(f"Import XTTS : OK | Xtts.inference : OK | GPT.generate : OK | PyTorch {torch.__version__}")
    if not args.sample:
        print("Vérification des imports terminée. Pour tester une génération réelle, ajoute --sample chemin/voix.wav.")
        return
    sample = Path(args.sample).expanduser().resolve()
    if not sample.is_file():
        raise SystemExit(f"Échantillon introuvable : {sample}")
    with tempfile.TemporaryDirectory(prefix="xtts-smoke-") as folder:
        output = Path(folder) / "smoke.wav"
        model = TTS("tts_models/multilingual/multi-dataset/xtts_v2", gpu=torch.cuda.is_available())
        model.tts_to_file(
            text="Bonjour, ceci est un test de Petit Studio Voix.",
            speaker_wav=str(sample), language="fr", file_path=str(output), split_sentences=True,
        )
        if not output.is_file() or output.stat().st_size < 1000:
            raise SystemExit("XTTS n’a pas créé un fichier WAV de test valide.")
        print(f"Synthèse XTTS réelle : OK ({output.stat().st_size:,} octets).")


if __name__ == "__main__":
    main()
