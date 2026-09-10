from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "resources"
SIZE = 256


def blend(left: tuple[int, int, int], right: tuple[int, int, int], ratio: float) -> tuple[int, int, int, int]:
    return tuple(round(left[index] + (right[index] - left[index]) * ratio) for index in range(3)) + (255,)


def create_icon() -> Image.Image:
    gradient = Image.new("RGBA", (SIZE, SIZE))
    pixels = gradient.load()
    start = (102, 126, 234)
    end = (118, 75, 162)
    for y in range(SIZE):
        for x in range(SIZE):
            pixels[x, y] = blend(start, end, (x + y) / (2 * (SIZE - 1)))

    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=54, fill=255)
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    image.paste(gradient, (0, 0), mask)

    draw = ImageDraw.Draw(image)
    points = [(48, 174), (91, 130), (125, 155), (190, 80)]
    draw.line(points, fill="white", width=18, joint="curve")
    for x, y in points[:-1]:
        draw.ellipse((x - 9, y - 9, x + 9, y + 9), fill="white")
    draw.line((161, 80, 190, 80, 190, 109), fill="white", width=18, joint="curve")
    return image


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    icon = create_icon()
    sizes = [(16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icon.save(OUTPUT / "app.ico", format="ICO", sizes=sizes)
    icon.save(OUTPUT / "tray.ico", format="ICO", sizes=sizes)
    icon.save(OUTPUT / "app-icon.png", format="PNG")


if __name__ == "__main__":
    main()
