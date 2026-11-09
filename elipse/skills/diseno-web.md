---
name: diseno-web
description: Landings y páginas HTML con diseño editorial cuidado (Tailwind, fotos, tipografía con carácter)
triggers: [página web, pagina web, sitio web, landing, website, web page, html, css, diseño web, diseno web, interfaz, dashboard, portafolio, portfolio, frontend, mockup, app web]
priority: 70
compact: |
  Diseño web: un .html con Tailwind por CDN y fuentes de Google (serif en títulos, sans en texto). Paleta clara y cálida (fondo #fdfbf7, texto #261a12) con UN acento del tema; oscuro solo si el tema lo pide. Define en tailwind.config todos los tonos que uses. Header fijo con CTA; hero de 2 columnas con foto grande rounded-3xl y tarjeta flotante con un dato; secciones py-24 con fondos alternos; tarjetas rounded-3xl; botones rounded-full con hover. Usa las imágenes del contexto, nunca cajas grises. Todo enlace del menú con su sección. Responsive.
---
# Diseño web con carácter

Entrega UN archivo .html completo (hasta </html>). Usa <script src="https://cdn.tailwindcss.com"></script> y una o dos fuentes de Google Fonts con fallback.

## Paleta (elegila según el tema)
- Por defecto: fondo crema claro (#fdfbf7), secciones alternas (#f4ede2), texto casi negro cálido (#261a12) y UN acento propio del tema. Oscuro solo si el tema lo pide (cine, gaming, nocturno).
- Acento por tema: café, terracota #b85d33; finanzas, verde #1f7a5a; tecnología, azul #2563eb; moda, un tono tierra. Sin degradados morado→azul ni neón.
- En tailwind.config definí TODOS los tonos que uses (50, 100, 200, 800, 900 y el acento 600/700). No uses una clase de color que no definiste.

## Tipografía
- Títulos en una serif con carácter (Playfair Display, Fraunces) o una sans geométrica (Plus Jakarta Sans); texto en una sans limpia.
- h1: text-5xl a text-7xl con leading-[1.1]; una palabra del h1 en cursiva o en el color del acento. Texto base de 17–18px con leading-relaxed y párrafos max-w-xl.
- Encima de cada título de sección, una etiqueta pequeña (text-xs, tracking-widest, uppercase) en el color del acento.

## Estructura de una landing
1. Header fijo translúcido (backdrop-blur) con logo, 3–4 enlaces y un botón CTA redondeado.
2. Hero a pantalla completa en 2 columnas: texto (7/12) y foto (5/12, aspect-[4/5], rounded-3xl, shadow-2xl) con una tarjeta flotante que muestre un dato. Dos botones: uno relleno y otro con borde.
3. Historia o propuesta: 2 columnas, fotos en grilla asimétrica y 2 cifras destacadas.
4. Catálogo o menú: grid md:2 lg:3, tarjetas rounded-3xl con título, precio, descripción y una etiqueta pill.
5. Contacto, horario o CTA final, y footer con columnas.
Secciones con py-24 o py-28 y fondos que se alternan.

## Imágenes
Usá las URLs de imagen que vengan en el contexto (object-cover, aspect fijo, alt descriptivo). NUNCA dejes una caja gris con texto entre corchetes: sin fotos, usá un gradiente suave o formas SVG del color del tema.

## Pulido
- Todo enlace del menú debe apuntar a una sección que EXISTE con ese id.
- Hover y focus en todo lo clicable (transition-colors), sombras suaves, botones rounded-full.
- Responsive: las columnas colapsan y hay un menú móvil simple. lang="es", <title> real.
- Contenido concreto del tema (nombres, precios, textos), sin lorem ipsum. Releé que no haya palabras cortadas.
