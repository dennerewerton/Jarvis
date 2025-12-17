UNO module (pasta `uno/`)

✅ Coloque esta pasta no MESMO nível do seu `site_bot.py`.

✅ Depois, no seu `site_bot.py` (depois de criar `app = Flask(...)`), adicione:

    from uno import register_uno
    register_uno(app)

✅ Você vai colocar a pasta de cartas aqui:
    uno/cartas_uno/

Rotas:
  /game/uno             -> lobby
  /game/uno/<mesa>      -> mesa
  /api/uno/*            -> API
  /cartas_uno/<arquivo> -> serve imagens de `uno/cartas_uno/`
