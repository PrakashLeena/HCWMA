"""Image tools for the Image Editing agent (build step 12; master document 4.3, 6.5, 9.4):
`image_info` (T1), `resize_image`, `crop_image`, `convert_format`, `apply_filter` (T2).

Deterministic edits with Pillow only. No model looks at the picture and nothing is generated.
Rules that hold for every tool here:

- **Originals are never touched.** Every edit writes a new file in `workspace/images/`, named
  after the source plus what was done (`beach_resized.jpg`). A name that exists gets a number
  (`beach_resized_2.jpg`); a file is never overwritten, and the output folder cannot be chosen
  by the model.
- Inputs are read from approved folders or the workspace; each path passes
  `PathPolicy.check(path, "read")` and the output passes `PathPolicy.check(path, "write")`
  (rule 10: protected paths, symlinks resolved first).
- The file type is checked from the file's own bytes, not its name; size, pixel count and
  output size are capped (a "decompression bomb" is refused); at most 10 files per call.
- Output files carry no metadata (no GPS position or camera data) and tool results never
  return text taken from inside an image (rule 13), only dimensions, format and size.
- One bad file does not stop a batch: each file gets its own result, errors are values.
"""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from pydantic import BaseModel, ConfigDict, Field, model_validator

from integrations.mcp.tool_schemas import (
    PermissionTier,
    ToolManifest,
    ToolPermissions,
    ToolSpec,
    ToolStatus,
)
from security.permissions.path_check import PathDenied, PathPolicy

IMAGE_DIR = "images"  # workspace/images/: where every edited copy goes
MAX_BATCH = 10
MAX_LISTING = 25
MAX_INPUT_BYTES = 50_000_000
MAX_PIXELS = 50_000_000
MAX_SIDE = 8000  # output width or height
READABLE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF"}
INPUT_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
# name the owner types -> (Pillow format, file extension)
OUTPUT_FORMATS = {
    "png": ("PNG", ".png"),
    "jpg": ("JPEG", ".jpg"),
    "webp": ("WEBP", ".webp"),
    "bmp": ("BMP", ".bmp"),
    "gif": ("GIF", ".gif"),
    "tiff": ("TIFF", ".tiff"),
}
_FORMAT_KEY = {
    "JPEG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
    "BMP": "bmp",
    "GIF": "gif",
    "TIFF": "tiff",
}

# Owner approved all five manifests on 2026-11-18 (add-tool skill, section 4). A new or
# changed image tool starts as "quarantined" again until the owner has seen it.
IMAGE_TOOLS_STATUS: ToolStatus = "approved"
_SCOPE = ToolPermissions(filesystem=["approved_folders", "workspace"])

# Pillow raises DecompressionBombError at twice this many pixels; we refuse earlier ourselves.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS * 2


class ToolRefused(RuntimeError):
    """The action cannot be done as asked. The message is safe to show to the agent."""


@dataclass(frozen=True)
class ImageToolDeps:
    policy: PathPolicy

    @property
    def out_dir(self) -> Path:
        out = self.policy.workspace / IMAGE_DIR
        out.mkdir(parents=True, exist_ok=True)
        return out.resolve()


# ---------- opening and saving ----------


@dataclass
class Opened:
    image: Image.Image
    path: Path
    format: str  # Pillow format of the source, e.g. "JPEG"


def _source(deps: ImageToolDeps, path: str) -> Path:
    try:
        real = deps.policy.check(path, "read")
    except PathDenied as exc:  # name the file, not its whole path: the message stays short
        raise ToolRefused(str(exc).replace(f"'{path}'", f"'{Path(path).name}'")) from exc
    if not real.exists():
        raise ToolRefused(f"there is no file '{Path(path).name}'")
    if not real.is_file():
        raise ToolRefused(f"'{path}' is a folder; give the path of one image file")
    if real.suffix.lower() not in INPUT_SUFFIXES:
        raise ToolRefused(
            f"'{real.name}' is not a supported image (jpg, png, webp, gif, bmp, tiff)"
        )
    if real.stat().st_size > MAX_INPUT_BYTES:
        raise ToolRefused(f"'{real.name}' is larger than {MAX_INPUT_BYTES // 1_000_000} MB")
    return real


def _header(real: Path) -> tuple[Image.Image, str]:
    """Open the image header only; the real format comes from the file's bytes."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(real)
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ToolRefused(f"'{real.name}' is not a readable image ({type(exc).__name__})") from exc
    fmt = img.format or ""
    if fmt not in READABLE_FORMATS:
        raise ToolRefused(f"'{real.name}' is a {fmt or 'unknown'} file, which is not supported")
    if img.width * img.height > MAX_PIXELS:
        raise ToolRefused(f"'{real.name}' has more than {MAX_PIXELS // 1_000_000} million pixels")
    return img, fmt


def _open(deps: ImageToolDeps, path: str) -> Opened:
    real = _source(deps, path)
    img, fmt = _header(real)
    try:
        img.load()
        img = ImageOps.exif_transpose(img)  # apply the camera's rotation, then drop the tag
    except (OSError, ValueError, SyntaxError) as exc:
        raise ToolRefused(f"'{real.name}' could not be decoded ({type(exc).__name__})") from exc
    if img.mode in ("P", "PA"):
        img = img.convert("RGBA" if "transparency" in img.info or img.mode == "PA" else "RGB")
    elif img.mode not in ("RGB", "RGBA", "L", "LA"):
        try:
            img = img.convert("RGB")
        except (ValueError, OSError) as exc:
            raise ToolRefused(f"'{real.name}' uses a colour mode that is not supported") from exc
    return Opened(img, real, fmt)


def _encode(img: Image.Image, fmt_key: str, quality: int) -> bytes:
    pil_format, _ = OUTPUT_FORMATS[fmt_key]
    out = img
    if pil_format == "JPEG" and img.mode in ("RGBA", "LA"):
        base = Image.new("RGB", img.size, (255, 255, 255))
        base.paste(img, mask=img.getchannel("A"))
        out = base
    elif pil_format == "JPEG" and img.mode not in ("RGB", "L"):
        out = img.convert("RGB")
    elif pil_format == "GIF":
        out = img.convert("RGBA" if img.mode in ("RGBA", "LA") else "RGB").convert(
            "P", palette=Image.Palette.ADAPTIVE
        )
    elif pil_format == "BMP" and img.mode in ("LA",):
        out = img.convert("RGBA")
    buffer = io.BytesIO()
    options: dict[str, object] = {}
    if pil_format in ("JPEG", "WEBP"):
        options["quality"] = quality
    if pil_format in ("JPEG", "PNG"):
        options["optimize"] = True
    out.save(buffer, format=pil_format, **options)  # no exif=, no icc: metadata is dropped
    return buffer.getvalue()


def _write_new(deps: ImageToolDeps, stem: str, tag: str, ext: str, data: bytes) -> Path:
    """Create `<stem>_<tag><ext>` in workspace/images/, adding _2, _3 ... if it exists."""
    out_dir = deps.out_dir
    base = f"{stem}_{tag}" if tag else stem
    for n in range(1, 100):
        name = f"{base}{ext}" if n == 1 else f"{base}_{n}{ext}"
        target = deps.policy.check(out_dir / name, "write")
        try:
            with target.open("xb") as handle:  # "x": fails instead of overwriting
                handle.write(data)
        except FileExistsError:
            continue
        return target
    raise ToolRefused("too many files with this name already exist in the images folder")


def _check_size(width: int, height: int) -> None:
    if width < 1 or height < 1:
        raise ToolRefused("the result would be empty")
    if width > MAX_SIDE or height > MAX_SIDE:
        raise ToolRefused(f"the result would be larger than {MAX_SIDE} pixels on a side")


# ---------- shared result shape ----------


class ItemResult(BaseModel):
    source: str
    output: str | None = None
    width: int | None = None
    height: int | None = None
    error: str | None = None


class EditOutput(BaseModel):
    results: list[ItemResult]
    done: int
    failed: int
    output_folder: str
    note: str = "Originals were not changed. Edited copies are in output_folder."


def _batch(deps: ImageToolDeps, paths: list[str], edit) -> EditOutput:  # type: ignore[no-untyped-def]
    """Run `edit(opened) -> (image, tag, fmt_key, quality)` over each file; errors are values."""
    results: list[ItemResult] = []
    for path in paths:
        try:
            opened = _open(deps, path)
            image, tag, fmt_key, quality = edit(opened)
            _check_size(*image.size)
            pil_format, ext = OUTPUT_FORMATS[fmt_key]
            data = _encode(image, fmt_key, quality)
            target = _write_new(deps, opened.path.stem, tag, ext, data)
            results.append(
                ItemResult(
                    source=str(opened.path),
                    output=str(target),
                    width=image.width,
                    height=image.height,
                )
            )
        except ToolRefused as exc:
            results.append(ItemResult(source=path, error=str(exc)))
        except (OSError, ValueError, MemoryError) as exc:
            results.append(ItemResult(source=path, error=f"failed: {type(exc).__name__}"))
    failed = sum(r.error is not None for r in results)
    if failed == len(results):  # nothing was made: report it as a failed call, not a success
        detail = "; ".join(str(r.error) for r in results[:3])
        more = f" (and {len(results) - 3} more)" if len(results) > 3 else ""
        raise ToolRefused(f"no image was edited. {detail}{more}")
    return EditOutput(
        results=results,
        done=len(results) - failed,
        failed=failed,
        output_folder=str(deps.out_dir),
    )


def _same_format(opened: Opened) -> str:
    return _FORMAT_KEY[opened.format]


_PATHS = Field(
    min_length=1,
    max_length=MAX_BATCH,
    description=f"Full paths of image files (at most {MAX_BATCH} per call).",
)


# ---------- image_info ----------


class ImageInfoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=500, description="An image file or a folder.")


class ImageInfo(BaseModel):
    path: str
    format: str
    width: int
    height: int
    mode: str
    size_bytes: int


class ImageInfoOutput(BaseModel):
    kind: Literal["image", "folder"]
    image: ImageInfo | None = None
    images: list[ImageInfo] | None = None
    count: int | None = None
    other_files: int | None = None
    more_available: bool = False
    note: str | None = None


def _info(real: Path) -> ImageInfo:
    img, fmt = _header(real)
    with img:
        return ImageInfo(
            path=str(real),
            format=fmt,
            width=img.width,
            height=img.height,
            mode=img.mode,
            size_bytes=real.stat().st_size,
        )


def _image_info(deps: ImageToolDeps, args: ImageInfoInput) -> ImageInfoOutput:
    try:
        real = deps.policy.check(args.path, "read")
    except PathDenied as exc:
        raise ToolRefused(str(exc)) from exc
    if not real.exists():
        raise ToolRefused(f"there is no file or folder '{args.path}'")
    if real.is_file():
        return ImageInfoOutput(kind="image", image=_info(_source(deps, args.path)))
    found: list[ImageInfo] = []
    other = 0
    entries = sorted(real.iterdir(), key=lambda p: p.name.lower())
    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() not in INPUT_SUFFIXES:
            other += entry.is_file()
            continue
        try:
            checked = deps.policy.check(entry, "read")
            found.append(_info(checked))
        except (PathDenied, ToolRefused):
            other += 1  # protected, broken or unsupported: not listed, not an error
    shown = found[:MAX_LISTING]
    return ImageInfoOutput(
        kind="folder",
        images=shown,
        count=len(found),
        other_files=other,
        more_available=len(found) > len(shown),
        note=None if found else "No supported image files (jpg, png, webp, gif, bmp, tiff) here.",
    )


# ---------- resize_image ----------


class ResizeImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = _PATHS
    width: int | None = Field(default=None, ge=1, le=MAX_SIDE, description="Target width in px.")
    height: int | None = Field(default=None, ge=1, le=MAX_SIDE, description="Target height in px.")
    exact: bool = Field(
        default=False,
        description="With both width and height: true stretches to exactly that size; "
        "false (default) keeps the proportions and fits inside it.",
    )

    @model_validator(mode="after")
    def _needs_a_size(self) -> ResizeImageInput:
        if self.width is None and self.height is None:
            raise ValueError("give a width, a height, or both")
        return self


def _resize_image(deps: ImageToolDeps, args: ResizeImageInput) -> EditOutput:
    def edit(o: Opened) -> tuple[Image.Image, str, str, int]:
        w, h = o.image.size
        if args.width and args.height and args.exact:
            size = (args.width, args.height)
        elif args.width and args.height:
            scale = min(args.width / w, args.height / h)
            size = (max(1, round(w * scale)), max(1, round(h * scale)))
        elif args.width:
            size = (args.width, max(1, round(h * args.width / w)))
        else:
            height = args.height or h
            size = (max(1, round(w * height / h)), height)
        _check_size(*size)
        return o.image.resize(size, Image.Resampling.LANCZOS), "resized", _same_format(o), 90

    return _batch(deps, args.paths, edit)


# ---------- crop_image ----------


class CropImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = _PATHS
    mode: Literal["square", "aspect", "box"] = Field(
        default="square",
        description="square: biggest centred square. aspect: biggest centred crop with the "
        "ratio in 'aspect'. box: the pixel rectangle left/top/right/bottom.",
    )
    aspect: str | None = Field(
        default=None, pattern=r"^[1-9][0-9]?:[1-9][0-9]?$", description="e.g. '16:9' or '4:5'."
    )
    left: int | None = Field(default=None, ge=0, le=MAX_SIDE)
    top: int | None = Field(default=None, ge=0, le=MAX_SIDE)
    right: int | None = Field(default=None, ge=1, le=MAX_SIDE)
    bottom: int | None = Field(default=None, ge=1, le=MAX_SIDE)

    @model_validator(mode="after")
    def _matches_mode(self) -> CropImageInput:
        box = (self.left, self.top, self.right, self.bottom)
        if self.mode == "aspect" and self.aspect is None:
            raise ValueError("mode 'aspect' needs aspect, e.g. '16:9'")
        if self.mode == "box":
            left, top, right, bottom = box
            if left is None or top is None or right is None or bottom is None:
                raise ValueError("mode 'box' needs left, top, right and bottom")
            if right <= left or bottom <= top:
                raise ValueError("right must be greater than left and bottom than top")
        return self


def _crop_box(args: CropImageInput, w: int, h: int) -> tuple[int, int, int, int]:
    if args.mode == "box":
        left, top, right, bottom = args.left, args.top, args.right, args.bottom
        if left is None or top is None or right is None or bottom is None:
            raise ToolRefused("a box needs left, top, right and bottom")
        if right > w or bottom > h:
            raise ToolRefused(f"the box goes outside the image, which is {w} x {h} pixels")
        return left, top, right, bottom
    ratio_w, ratio_h = (
        (1, 1) if args.mode == "square" else map(int, (args.aspect or "1:1").split(":"))
    )
    scale = min(w / ratio_w, h / ratio_h)
    cw, ch = max(1, int(ratio_w * scale)), max(1, int(ratio_h * scale))
    left, top = (w - cw) // 2, (h - ch) // 2
    return left, top, left + cw, top + ch


def _crop_image(deps: ImageToolDeps, args: CropImageInput) -> EditOutput:
    def edit(o: Opened) -> tuple[Image.Image, str, str, int]:
        box = _crop_box(args, *o.image.size)
        return o.image.crop(box), "cropped", _same_format(o), 90

    return _batch(deps, args.paths, edit)


# ---------- convert_format ----------


class ConvertFormatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = _PATHS
    format: Literal["png", "jpg", "webp", "bmp", "gif", "tiff"]
    quality: int = Field(default=90, ge=10, le=100, description="For jpg and webp.")


def _convert_format(deps: ImageToolDeps, args: ConvertFormatInput) -> EditOutput:
    def edit(o: Opened) -> tuple[Image.Image, str, str, int]:
        return o.image, "", args.format, args.quality

    return _batch(deps, args.paths, edit)


# ---------- apply_filter ----------

FilterName = Literal[
    "grayscale", "sepia", "invert", "blur", "sharpen", "autocontrast", "brightness", "contrast"
]
# filter -> (what `amount` means, default, lowest, highest)
_AMOUNTS: dict[str, tuple[float, float, float]] = {
    "blur": (2.0, 0.5, 20.0),  # radius in px
    "sharpen": (2.0, 0.0, 5.0),  # 1 = unchanged, 2 = twice as sharp
    "brightness": (1.2, 0.1, 3.0),  # 1 = unchanged
    "contrast": (1.2, 0.1, 3.0),  # 1 = unchanged
}


class ApplyFilterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = _PATHS
    filter: FilterName
    amount: float | None = Field(
        default=None,
        description="blur: radius in px (default 2). sharpen / brightness / contrast: factor "
        "where 1 = unchanged (default 2 / 1.2 / 1.2). Ignored by the other filters.",
    )

    @model_validator(mode="after")
    def _amount_in_range(self) -> ApplyFilterInput:
        if self.amount is not None and self.filter in _AMOUNTS:
            _, low, high = _AMOUNTS[self.filter]
            if not low <= self.amount <= high:
                raise ValueError(f"amount for {self.filter} must be between {low} and {high}")
        return self


def _split_alpha(img: Image.Image) -> tuple[Image.Image, Image.Image | None]:
    if img.mode in ("RGBA", "LA"):
        return img.convert("RGB") if img.mode == "RGBA" else img.getchannel("L"), img.getchannel(
            "A"
        )
    return img, None


def _filtered(img: Image.Image, name: str, amount: float | None) -> Image.Image:
    base, alpha = _split_alpha(img)
    default = _AMOUNTS.get(name, (0.0, 0.0, 0.0))[0]
    value = default if amount is None else amount
    if name == "grayscale":
        out = ImageOps.grayscale(base)
    elif name == "sepia":
        out = ImageOps.colorize(
            ImageOps.grayscale(base), black=(30, 16, 0), white=(255, 240, 200), mid=(160, 112, 66)
        )
    elif name == "invert":
        out = ImageOps.invert(base.convert("RGB"))
    elif name == "blur":
        out = base.filter(ImageFilter.GaussianBlur(radius=value))
    elif name == "sharpen":
        out = ImageEnhance.Sharpness(base).enhance(value)
    elif name == "autocontrast":
        out = ImageOps.autocontrast(base)
    elif name == "brightness":
        out = ImageEnhance.Brightness(base).enhance(value)
    else:
        out = ImageEnhance.Contrast(base).enhance(value)
    if alpha is not None:
        out = out.convert("RGB") if out.mode != "L" else out
        out.putalpha(alpha)
    return out


def _apply_filter(deps: ImageToolDeps, args: ApplyFilterInput) -> EditOutput:
    def edit(o: Opened) -> tuple[Image.Image, str, str, int]:
        return _filtered(o.image, args.filter, args.amount), args.filter, _same_format(o), 90

    return _batch(deps, args.paths, edit)


# ---------- registration ----------


def image_tool_specs(deps: ImageToolDeps, status: ToolStatus = "quarantined") -> list[ToolSpec]:
    def spec(
        name: str,
        description: str,
        tier: PermissionTier,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        handler: object,
        *,
        writes: bool,
    ) -> ToolSpec:
        manifest = ToolManifest(
            name=name,
            version="0.1.0",
            created_by="developer",
            status=status,
            route="pc",
            description=description,
            permission_tier=tier,
            side_effects="writes" if writes else "none",
            permissions=_SCOPE,
            timeout_s=120 if writes else 30,
        )
        return ToolSpec(
            manifest=manifest,
            input_model=input_model,
            output_model=output_model,
            handler=handler,  # type: ignore[arg-type]
        )

    return [
        spec(
            "image_info",
            "Show the size, format and file size of one image, or list the images in a "
            "folder with their sizes. Use it first to get the full paths of the images.",
            PermissionTier.T1,
            ImageInfoInput,
            ImageInfoOutput,
            lambda a: _image_info(deps, a),
            writes=False,
        ),
        spec(
            "resize_image",
            "Make a smaller or larger COPY of up to 10 images with a new width and/or height, "
            "keeping proportions. The originals are never changed; copies go to the "
            "workspace images folder.",
            PermissionTier.T2,
            ResizeImageInput,
            EditOutput,
            lambda a: _resize_image(deps, a),
            writes=True,
        ),
        spec(
            "crop_image",
            "Save a cropped COPY of up to 10 images: a centred square, a centred crop with "
            "a ratio like 16:9, or an exact pixel box. Originals are never changed.",
            PermissionTier.T2,
            CropImageInput,
            EditOutput,
            lambda a: _crop_image(deps, a),
            writes=True,
        ),
        spec(
            "convert_format",
            "Save a COPY of up to 10 images in another format (png, jpg, webp, bmp, gif, "
            "tiff). Originals are never changed.",
            PermissionTier.T2,
            ConvertFormatInput,
            EditOutput,
            lambda a: _convert_format(deps, a),
            writes=True,
        ),
        spec(
            "apply_filter",
            "Save a filtered COPY of up to 10 images: grayscale, sepia, invert, blur, "
            "sharpen, autocontrast, brightness or contrast. Originals are never changed.",
            PermissionTier.T2,
            ApplyFilterInput,
            EditOutput,
            lambda a: _apply_filter(deps, a),
            writes=True,
        ),
    ]


def default_image_tool_specs() -> list[ToolSpec]:
    """The image tools wired to `configs/permissions.yaml`."""
    return image_tool_specs(ImageToolDeps(PathPolicy.from_config()), status=IMAGE_TOOLS_STATUS)
