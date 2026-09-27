# bandera_mexico.py
# Dibujar la bandera de México en la terminal con colores ANSI

# Códigos de colores ANSI
RED = "\033[41m"      # fondo rojo
WHITE = "\033[47m"    # fondo blanco
GREEN = "\033[42m"    # fondo verde
RESET = "\033[0m"     # volver al color por defecto

# Dimensiones de la bandera (puedes ajustar el ancho y alto)
WIDTH = 30   # ancho total de la bandera
HEIGHT = 9   # número de filas (debe ser múltiplo de 3 para que cada franja tenga la misma altura)

def draw_stripe(color: str, rows: int) -> None:
    """Imprime `rows` filas de una franja del color indicado."""
    for _ in range(rows):
        # Cada fila se compone de WIDTH espacios con fondo de color
        print(color + " " * WIDTH + RESET)

def main() -> None:
    # Cada franja ocupa un tercio de la altura
    stripe_height = HEIGHT // 3

    # Franja verde (izquierda)
    for _ in range(stripe_height):
        # En la bandera real la franja verde está a la izquierda,
        # pero en terminal imprimimos toda la fila con el mismo color.
        # Para simular la división vertical usamos bloques de color:
        green_part = GREEN + " " * (WIDTH // 3) + RESET
        white_part = WHITE + " " * (WIDTH // 3) + RESET
        red_part   = RED   + " " * (WIDTH // 3) + RESET
        print(green_part + white_part + red_part)

    # Franja blanca (centro) – aquí normalmente iría el escudo,
    # pero lo sustituimos por una fila blanca completa.
    draw_stripe(WHITE, stripe_height)

    # Franja roja (derecha)
    draw_stripe(RED, stripe_height)

if __name__ == "__main__":
    main()
