"""Bounded image decoding; no S3 or database operations here."""
import io
import warnings
from django.conf import settings
from PIL import Image, ImageOps, UnidentifiedImageError

IMAGE_TYPES = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


class InvalidImage(Exception):
    pass


def optimize_image(data, content_type):
    if content_type not in IMAGE_TYPES or not data or len(data) > settings.MEDIA_MAX_BYTES:
        raise InvalidImage("invalid_image")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=list(IMAGE_TYPES.values())) as probe:
                if (probe.format != IMAGE_TYPES[content_type] or probe.width * probe.height > settings.MEDIA_IMAGE_MAX_PIXELS
                        or getattr(probe, "n_frames", 1) != 1):
                    raise InvalidImage("invalid_image")
                probe.verify()
            with Image.open(io.BytesIO(data), formats=list(IMAGE_TYPES.values())) as source:
                image = ImageOps.exif_transpose(source)
                image.thumbnail((settings.MEDIA_IMAGE_MAX_DIMENSION,) * 2, Image.Resampling.LANCZOS)
                # Preserve transparency in WebP, flatten onto white for JPEG.
                if settings.MEDIA_IMAGE_OUTPUT_FORMAT == "JPEG" and ("A" in image.getbands() or image.mode == "P"):
                    rgba = image.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    image = background
                else:
                    has_alpha = "A" in image.getbands() or (image.mode == "P" and "transparency" in image.info)
                    image = image.convert("RGBA" if settings.MEDIA_IMAGE_OUTPUT_FORMAT == "WEBP" and has_alpha else "RGB")
                # Do not pass through EXIF/GPS/comments into the derivative.
                output = io.BytesIO()
                image.save(output, format=settings.MEDIA_IMAGE_OUTPUT_FORMAT, quality=settings.MEDIA_IMAGE_QUALITY,
                           **({"optimize": True} if settings.MEDIA_IMAGE_OUTPUT_FORMAT == "JPEG" else {"method": 4}))
                return output.getvalue(), "image/jpeg" if settings.MEDIA_IMAGE_OUTPUT_FORMAT == "JPEG" else "image/webp", image.width, image.height
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InvalidImage("invalid_image") from exc
