from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from config.settings import get_settings
from src.image_prompt_generator import ImagePromptGenerator

logger = logging.getLogger(__name__)

_VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


class ImageGenerator:
    """Generates AI image prompts, then waits for the user to create
    and place images manually before continuing the pipeline.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.prompt_gen = ImagePromptGenerator()

    def generate_images(
        self,
        script: str,
        topic: str,
        content: str,
        video_id: int,
        num_prompts: int = 8,
    ) -> list[Path]:
        start = time.monotonic()
        project = self.settings.project_root

        # ── 1. Generate prompts ────────────────────────────────────
        logger.info(
            "Generating %d image prompts from topic + content + script",
            num_prompts,
        )
        prompts = self.prompt_gen.generate(
            topic, content, script, num_prompts=num_prompts
        )

        # ── 2. Save prompts to file ────────────────────────────────
        prompts_dir = project / "image_prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        prompts_path = prompts_dir / f"{video_id}_prompts.txt"
        with open(prompts_path, "w", encoding="utf-8") as f:
            for i, p in enumerate(prompts, 1):
                f.write(f"{i}. {p}\n\n")
        logger.info("Saved %d prompts to %s", len(prompts), prompts_path)

        # ── 3. Create asset directory ──────────────────────────────
        asset_dir = project / "assets" / f"topic_{video_id}"
        asset_dir.mkdir(parents=True, exist_ok=True)

        # ── 4. Folder validation log ───────────────────────────────
        real_images: list[Path] = []
        placeholder_images: list[Path] = []
        if asset_dir.exists():
            for f in sorted(asset_dir.iterdir(), key=lambda p: p.name.lower()):
                if f.is_dir():
                    continue
                if f.suffix.lower() in _VALID_EXTENSIONS:
                    if "_placeholder." in f.name:
                        placeholder_images.append(f)
                    else:
                        real_images.append(f)

        logger.info(
            "Folder validation: "
            "expected=%s actual=%s real=%d placeholder=%d",
            asset_dir, asset_dir, len(real_images), len(placeholder_images),
        )
        for p in real_images:
            logger.info("  REAL: %s", p.name)
        for p in placeholder_images:
            logger.info("  PLACEHOLDER: %s", p.name)

        # ── 5. Pause and wait for manual image placement ───────────
        print()
        print("=" * 60)
        print("  HUMAN INTERVENTION REQUIRED")
        print("=" * 60)
        print(f"  Generated {len(prompts)} image prompts.")
        print()
        print("  Create images using the prompts and place them in:")
        print()
        print(f"    {asset_dir}")
        print()
        print("  Supported formats:")
        print("    .jpg")
        print("    .jpeg")
        print("    .png")
        print("    .webp")
        print()
        print(f"  Minimum required images: {num_prompts}")
        print()
        input("  Press ENTER when finished.")
        print("=" * 60)
        print()

        # ── 6. Validate ────────────────────────────────────────────
        images = self._validate_assets(asset_dir, min_count=num_prompts)
        logger.info("Validation: %d images found in %s", len(images), asset_dir)

        # ── 7. Frame-level log with dimensions ─────────────────────
        for i, path in enumerate(images):
            dims = self._get_dimensions(path)
            logger.info(
                "FRAME %03d  Selected: %s  Dimensions: %s  Type: %s",
                i + 1, path.name, dims, path.suffix.lower(),
            )
            # Log any skipped placeholder
            placeholder_candidate = path.parent / f"{path.stem}_placeholder{path.suffix}"
            if placeholder_candidate.exists():
                logger.info(
                    "  Skipped: %s  Reason: Real image exists",
                    placeholder_candidate.name,
                )

        # ── 8. Return stable paths ─────────────────────────────────
        stable = self._to_stable_paths(images, video_id)
        duration = time.monotonic() - start

        # ── 9. Debug report ────────────────────────────────────────
        print()
        print("=" * 60)
        print("  IMAGE INGESTION REPORT")
        print("=" * 60)
        print(f"  Images used:       {len(stable)}")
        placeholders_found = len([p for p in stable if "_placeholder." in p.name])
        print(f"  Placeholders used: {placeholders_found}")
        print(f"  Asset folder:      {asset_dir}")
        print(f"  Frames folder:     {stable[0].parent if stable else '(none)'}")
        print("=" * 60)
        print()

        logger.info(
            "Image step complete: %d images in %.1fs",
            len(stable), duration,
        )
        return stable

    # ── validation ─────────────────────────────────────────────────

    def _validate_assets(
        self, directory: Path, min_count: int = 6
    ) -> list[Path]:
        if not directory.exists():
            raise FileNotFoundError(
                f"Asset directory does not exist: {directory}\n"
                f"Create it and place your images inside before continuing."
            )

        all_files = sorted(
            directory.iterdir(), key=lambda p: p.name.lower()
        )
        images: list[Path] = []
        unsupported: list[str] = []
        corrupt: list[str] = []

        for f in all_files:
            if f.is_dir():
                continue
            ext = f.suffix.lower()
            if ext not in _VALID_EXTENSIONS:
                unsupported.append(f.name)
                continue

            ok = self._try_open(f)
            status = "PASS" if ok else "FAIL"
            logger.info("  %s %s", f.name, status)
            if ok:
                images.append(f)
            else:
                corrupt.append(f.name)

        if unsupported:
            print()
            print("Unsupported format:")
            for name in unsupported:
                print(f"  {name}")
            print()
            raise RuntimeError(
                f"Unsupported format(s) found. "
                f"Only {', '.join(sorted(_VALID_EXTENSIONS))} allowed."
            )

        if corrupt:
            print()
            print("Corrupt or unreadable images:")
            for name in corrupt:
                print(f"  {name}")
            print()
            raise RuntimeError(
                f"{len(corrupt)} image(s) could not be opened. "
                f"Replace or recreate them."
            )

        if len(images) < min_count:
            print()
            print("Missing images:")
            print(f"  Required: {min_count}")
            print(f"  Found:    {len(images)}")
            print()
            raise RuntimeError(
                f"Only {len(images)} valid images found in {directory}, "
                f"need at least {min_count}.\n"
                f"Place {min_count} or more .jpg/.jpeg/.png/.webp files "
                f"and re-run."
            )

        return images[:min_count]

    @staticmethod
    def _try_open(path: Path) -> bool:
        try:
            with Image.open(path) as img:
                img.verify()
            return True
        except Exception:
            return False

    @staticmethod
    def _get_dimensions(path: Path) -> str:
        try:
            with Image.open(path) as img:
                return f"{img.width}x{img.height}"
        except Exception:
            return "unknown"

    # ── stable naming ──────────────────────────────────────────────

    @staticmethod
    def _to_stable_paths(
        images: list[Path], video_id: int
    ) -> list[Path]:
        """Copy images to assets/topic_<video_id>/frames/ with stable
        names so the video assembler finds them in order.

        Priority rule: if ``frame_NNN.jpg`` exists, NEVER use
        ``frame_NNN_placeholder.*``.
        """
        frames_dir = images[0].parent / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        # Scan for *existing* frame files so we know what to skip.
        existing_stable: set[str] = set()
        for f in frames_dir.iterdir():
            if f.suffix.lower() in _VALID_EXTENSIONS:
                existing_stable.add(f.stem)

        stable: list[Path] = []
        for i, src in enumerate(images):
            stem = f"frame_{i+1:03d}"
            dst = frames_dir / f"{stem}{src.suffix}"

            # If a real image already exists with this stem, skip placeholders.
            if stem in existing_stable and "_placeholder" in src.stem:
                logger.warning(
                    "  PLACEHOLDER SKIPPED: %s -> %s (real image exists)",
                    src.name, dst.name,
                )
                continue

            dst.write_bytes(src.read_bytes())
            stable.append(dst)
            log_line = "REAL" if "_placeholder" not in src.stem else "PLACEHOLDER"
            logger.info(
                "  Using %s: %s -> %s", log_line, src.name, dst.name,
            )

        # If we ended up with fewer frames than images after placeholder
        # filtering, fall back to whatever real images we have.
        if len(stable) < len(images):
            missing = len(images) - len(stable)
            logger.warning(
                "%d placeholder(s) skipped, using %d real images",
                missing, len(stable),
            )

        return stable
