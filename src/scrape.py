"""
Etapa 1 — coleta de dados de tabuademares.com/br/paraiba/joao-pessoa.

    mares_<ano>.csv      tábua de marés do ano (3-4 marés por dia)
    mares_previsao.csv   marés do mês corrente
    ondas.csv            altura de onda hora a hora (~7 dias à frente)
    vento.csv            vento hora a hora (~7 dias à frente)

Rodar com: uv run python src/scrape.py
"""

import re
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

URL_BASE = "https://tabuademares.com/br/paraiba/joao-pessoa"
URL_ONDAS = f"{URL_BASE}/previsao/ondas"
URL_VENTO = f"{URL_BASE}/previsao/vento"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

PAUSA = 1.5

DIR_RAW = Path(__file__).resolve().parent.parent / "data" / "raw"

MESES_ABREV = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN",
               "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]

RE_HORA = re.compile(r"(?<![\d:])(\d{1,2}:\d{2})(?![\d:])")
RE_DIA_ONCLICK = re.compile(r"Day\('(\d{4})-(\d{1,2})-(\d{1,2})'\)")
RE_FASE = re.compile(r"^icon-hs(\d+)$")
RE_DIA_LINK = re.compile(r"Day\('(\d{4})-(\d{1,2})-(\d{1,2})'\)")
RE_VALOR_UNIDADE = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*([A-Za-zº°%]+(?:/[A-Za-z]+)?)?")

RE_CABECALHO_DIA = re.compile(
    r"^(\d{1,2})\s*(" + "|".join(MESES_ABREV) + r")\s*$", re.MULTILINE
)


def _num(texto: str) -> float:
    return float(texto.replace(",", "."))





def baixar_html(url: str, dados_post: dict | None = None) -> str:
    """Baixa uma página e devolve o HTML (GET, ou POST se houver `dados_post`)."""
    if dados_post is None:
        resp = requests.get(url, headers=HEADERS, timeout=30)
    else:
        resp = requests.post(url, headers=HEADERS, data=dados_post, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text



def _campos_mes(ano: int, mes: int) -> dict:
    """Payload do form#form_calendario (POST em URL_BASE): fecha=AAAA-MM-DD."""
    return {"fecha": f"{ano}-{mes:02d}-01"}


def _texto(tag, seletor: str) -> str | None:
    el = tag.select_one(seletor)
    return el.get_text(strip=True) if el else None


def parsear_mares(html: str, ano: int, mes: int) -> list[dict]:
    """Uma linha por maré. Colunas: data, hora, altura_m, tipo (alta/baixa),
    coeficiente, coef_rotulo, nascer_sol, por_sol, fase_lunar (1-30, 1 = nova),
    atividade (0-3 peixes)."""
    soup = BeautifulSoup(html, "html.parser")
    tabela = soup.find("table", id="tabla_mareas")
    if tabela is None:
        raise ValueError("table#tabla_mareas não encontrada: layout mudou?")

    eventos = []
    for tr in tabela.find_all("tr", onclick=RE_DIA_ONCLICK):
        a, m, d = map(int, RE_DIA_ONCLICK.search(tr["onclick"]).groups())
        if (a, m) != (ano, mes):
            raise ValueError(
                f"Página trouxe {a}-{m:02d}, esperado {ano}-{mes:02d}: "
                "a troca de mês (POST) não funcionou."
            )
        data = date(a, m, d).isoformat()

        coef_tag = tr.select_one(".tabla_mareas_coeficiente_numero")
        coef_txt = coef_tag.find(string=True, recursive=False) if coef_tag else None
        coef = re.search(r"\d+", coef_txt) if coef_txt else None

        lua = tr.find("span", class_=RE_FASE)
        fase = None
        if lua:
            fase = int(next(RE_FASE.match(c).group(1) for c in lua["class"] if RE_FASE.match(c)))

        comuns = {
            "data": data,
            "coeficiente": int(coef.group()) if coef else None,
            "coef_rotulo": _texto(tr, ".tabla_mareas_coeficiente_altura"),
            "nascer_sol": _texto(tr, ".tabla_mareas_salida_puesta_sol_salida"),
            "por_sol": _texto(tr, ".tabla_mareas_salida_puesta_sol_puesta"),
            "fase_lunar": fase,
            "atividade": len(tr.select("span.icon-ic_pez_leyenda")),
        }

        for td in tr.select("td.tabla_mareas_marea"):
            hora = _texto(td, ".tabla_mareas_marea_hora")
            if hora is None: 
                continue
            h, mi = hora.split(":")
            eventos.append({
                "data": comuns["data"],
                "hora": f"{int(h):02d}:{mi}",
                "altura_m": _num(_texto(td, ".tabla_mareas_marea_altura_numero")),
                "tipo": "alta" if td.select_one(".tabla_mareas_marea_pleamar") else "baixa",
                **{k: v for k, v in comuns.items() if k != "data"},
            })

    if not eventos:
        raise ValueError("Nenhuma maré encontrada na tabela.")
    return eventos

def parsear_previsao(html: str, ano: int) -> list[dict]:
    """Leituras horárias das páginas de previsão de vento e de ondas.

    Colunas: data, hora, direcao, valor, unidade.
    A data vem do link '+ INFO' de cada dia (Day('AAAA-M-D')), então o ano
    não precisa ser inferido; `ano` só é usado se o link faltar.
    """
    soup = BeautifulSoup(html, "html.parser")
    fichas = soup.select("div.ficha")
    if not fichas:
        raise ValueError("Nenhum div.ficha encontrado: layout mudou?")

    registros = []
    ano_atual, mes_anterior = ano, None
    for ficha in fichas:
        link = ficha.select_one("a.a_enlace")
        m = RE_DIA_LINK.search(link.get("href", "")) if link else None
        if m:
            d = date(*map(int, m.groups()))
        else: 
            dia = int(_texto(ficha, "span.dia"))
            mes = MESES_ABREV.index(_texto(ficha, "span.mes").upper()) + 1
            if mes_anterior is not None and mes < mes_anterior:
                ano_atual += 1
            mes_anterior = mes
            d = date(ano_atual, mes, dia)

        linhas = 0
        for row in ficha.select("div.f_temp_horas"):
            celulas = [c.get_text(strip=True) for c in row.select("div.f_temp_hora")]
            barra = row.select_one("div.grafico_temp_barra_relleno")
            if not celulas or barra is None:
                continue
            mv = RE_VALOR_UNIDADE.search(barra.get_text(strip=True))
            if not mv:
                continue
            h, mi = celulas[0].split(":")
            registros.append({
                "data": d.isoformat(),
                "hora": f"{int(h):02d}:{mi}",
                "direcao": celulas[1] if len(celulas) > 1 else None,
                "valor": _num(mv.group(1)),
                "unidade": mv.group(2),
            })
            linhas += 1
        if linhas != 24:
            print(f"  aviso: {d} tem {linhas} linhas horárias (esperado 24)")

    return registros


def coletar_mares_do_ano(ano: int) -> pd.DataFrame:
    """Junta os 12 meses da tábua de marés de `ano` num DataFrame."""
    partes = []
    for mes in range(1, 13):
        html = baixar_html(URL_BASE, dados_post=_campos_mes(ano, mes))
        linhas = parsear_mares(html, ano, mes)
        print(f"  {ano}-{mes:02d}: {len(linhas)} marés")
        partes.append(pd.DataFrame(linhas))
        time.sleep(PAUSA)
    return pd.concat(partes, ignore_index=True)


def _salvar(df: pd.DataFrame, nome: str) -> None:
    if df.empty:
        raise RuntimeError(f"{nome} ficou vazio, não vou gravar.")
    df.to_csv(DIR_RAW / nome, index=False, encoding="utf-8")
    print(f"{nome}: {len(df)} linhas ({df['data'].min()} a {df['data'].max()})")


def main(ano: int = 2025) -> None:
    DIR_RAW.mkdir(parents=True, exist_ok=True)
    hoje = datetime.now()

    _salvar(coletar_mares_do_ano(ano), f"mares_{ano}.csv")

    html = baixar_html(URL_BASE)
    _salvar(pd.DataFrame(parsear_mares(html, hoje.year, hoje.month)), "mares_previsao.csv")
    time.sleep(PAUSA)

    df_ondas = pd.DataFrame(parsear_previsao(baixar_html(URL_ONDAS), hoje.year))
    _salvar(df_ondas.rename(columns={"valor": "altura_m"}), "ondas.csv")
    time.sleep(PAUSA)

    df_vento = pd.DataFrame(parsear_previsao(baixar_html(URL_VENTO), hoje.year))
    _salvar(df_vento.rename(columns={"valor": "velocidade"}), "vento.csv")




if __name__ == "__main__":
    main()