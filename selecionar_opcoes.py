"""
Seletor rápido de opções no dinheiro — autossuficiente.

Este arquivo não depende do repositório do coletor: traz embutido o cliente
da brapi e a normalização de que precisa. Assim o repositório "Opcoes-Agora"
fica independente, e pode virar público mais tarde (para o Streamlit) sem
carregar dado nem credencial.

Duas origens de preço, de propósito:
  - o ATIVO vem do endpoint de cotação, atualizado durante o pregão com
    alguns minutos de atraso — define a faixa e a distância ao dinheiro;
  - as OPÇÕES vêm da cadeia, que é do último pregão fechado.

Uso:
    python selecionar_opcoes.py --underlying PETR4 --html saida.html
"""

from __future__ import annotations

import argparse
import calendar
import html as _html
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

log = logging.getLogger("seletor")

BASE = "https://brapi.dev/api/v2/options"
QUOTE_URL = "https://brapi.dev/api/quote/{ticker}"

COLUMN_ALIASES: dict[str, str] = {
    "impliedVolatility": "iv",
    "implied_volatility": "iv",
    "volatility": "iv",
    "expirationDate": "expiration",
    "expiration_date": "expiration",
    "dueDate": "expiration",
    "strikePrice": "strike",
    "openInterest": "open_interest",
    "open_interest": "open_interest",
    "coveredPositions": "oi_coberta",
    "uncoveredPositions": "oi_descoberta",
    "blockedPositions": "oi_bloqueada",
    "financialVolume": "financial_volume",
    "referencePrice": "reference_price",
    "optionPrice": "option_price",
    "underlyingPrice": "spot_payload",
    "impliedVolatility": "iv",
    "type": "side",
    "optionType": "side",
}

NUMERIC = [
    "strike", "close", "open", "high", "low", "average", "bid", "ask",
    "volume", "financial_volume", "trades", "open_interest",
    "oi_coberta", "oi_descoberta", "oi_bloqueada",
    "iv", "delta", "gamma", "theta", "vega", "rho", "reference_price",
]


class BrapiError(RuntimeError):
    pass


@dataclass
class BrapiClient:
    """Cliente com retry exponencial. Sem token, só PETR4 responde (sandbox)."""

    token: str | None = None
    timeout: int = 30
    max_retries: int = 4
    pause: float = 0.35  # respiro entre chamadas

    def __post_init__(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "coletor-opcoes/1.0"
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

    def _get(self, url: str, **params: Any) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        delay = 1.0
        last = None
        for attempt in range(1, self.max_retries + 1):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                if r.status_code == 200:
                    time.sleep(self.pause)
                    return r.json()
                if r.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {r.status_code}"
                    log.warning("%s em %s (tentativa %d) — aguardando %.1fs",
                                last, url, attempt, delay)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise BrapiError(f"HTTP {r.status_code} em {url}: {r.text[:300]}")
            except requests.RequestException as exc:
                last = str(exc)
                log.warning("Falha de rede (tentativa %d): %s", attempt, exc)
                time.sleep(delay)
                delay *= 2
        raise BrapiError(f"Esgotou {self.max_retries} tentativas em {url}: {last}")

    def expirations(self, underlying: str) -> dict:
        return self._get(f"{BASE}/expirations", underlying=underlying)

    def chain(self, underlying: str, expiration: str, **kw: Any) -> dict:
        return self._get(f"{BASE}/chain", underlying=underlying,
                         expirationDate=expiration, **kw)

    def analytics(self, underlying: str, expiration: str,
                  on: str | None = None) -> dict:
        return self._get(f"{BASE}/analytics", underlying=underlying,
                         expirationDate=expiration, date=on)

    def positions(self, underlying: str, expiration: str,
                  on: str | None = None) -> dict:
        return self._get(f"{BASE}/positions", underlying=underlying,
                         expirationDate=expiration, date=on)

    def spot(self, ticker: str) -> float | None:
        try:
            payload = self._get(QUOTE_URL.format(ticker=ticker))
        except BrapiError as exc:
            log.warning("Não consegui o spot de %s: %s", ticker, exc)
            return None
        for rec in _records(payload):
            for key in ("regularMarketPrice", "close", "price"):
                if rec.get(key) is not None:
                    return float(rec[key])
        return None


def _records(payload: Any) -> list[dict]:
    """Extrai a lista de registros sem depender do nome da chave de topo."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "options", "data", "chain", "analytics",
                "positions", "expirations", "strikes"):
        val = payload.get(key)
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val
        # brapi às vezes aninha: results[0].options
        if isinstance(val, list) and val and isinstance(val[0], dict) is False:
            continue
    for val in payload.values():
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val
        if isinstance(val, dict):
            nested = _records(val)
            if nested:
                return nested
    return []


def _scalars(payload: Any) -> list:
    """Extrai lista de escalares (usado por /expirations, que devolve datas)."""
    if isinstance(payload, list):
        return [v for v in payload if not isinstance(v, (dict, list))]
    if isinstance(payload, dict):
        for val in payload.values():
            if isinstance(val, list) and val and not isinstance(val[0], (dict, list)):
                return val
            if isinstance(val, (dict, list)):
                nested = _scalars(val)
                if nested:
                    return nested
    return []


def parse_expirations(payload: Any) -> list[str]:
    dates = _scalars(payload)
    if dates:
        return sorted({str(d)[:10] for d in dates})
    out = set()
    for rec in _records(payload):
        for key in ("expirationDate", "expiration", "date", "dueDate"):
            if rec.get(key):
                out.add(str(rec[key])[:10])
    return sorted(out)


def normalize(payload: Any, *, underlying: str, expiration: str,
              trade_date: str) -> pd.DataFrame:
    recs = _records(payload)
    if not recs:
        return pd.DataFrame()
    df = pd.json_normalize(recs)
    df = df.rename(columns={k: v for k, v in COLUMN_ALIASES.items() if k in df.columns})

    df["underlying"] = underlying
    df["expiration"] = df.get("expiration", pd.Series([expiration] * len(df)))
    df["expiration"] = df["expiration"].astype(str).str[:10]
    # a coluna "date" do payload é a data do pregão; a de execução não serve
    if "date" in df.columns:
        df["trade_date"] = df["date"].astype(str).str[:10]
    else:
        df["trade_date"] = trade_date

    for col in NUMERIC:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "side" in df.columns:
        df["side"] = (df["side"].astype(str).str.lower()
                      .str.replace("compra", "call", regex=False)
                      .str.replace("venda", "put", regex=False)
                      .str[:4])
    elif "symbol" in df.columns:
        df["side"] = df["symbol"].map(side_from_symbol)

    return df


def side_from_symbol(symbol: str | None) -> str | None:
    """Padrão B3: letra do mês A-L = call, M-X = put."""
    if not isinstance(symbol, str) or len(symbol) < 5:
        return None
    letter = symbol[4].upper()
    if "A" <= letter <= "L":
        return "call"
    if "M" <= letter <= "X":
        return "put"
    return None


def enrich(df: pd.DataFrame, spot: float | None) -> pd.DataFrame:
    """Mid do book, spread relativo e moneyness — a base do filtro de liquidez."""
    if df.empty:
        return df
    out = df.copy()
    bid = out.get("bid")
    ask = out.get("ask")
    if bid is not None and ask is not None:
        valid = (bid > 0) & (ask > 0) & (ask >= bid)
        out["mid"] = ((bid + ask) / 2).where(valid)
        out["spread_pct"] = ((ask - bid) / out["mid"]).where(valid)
    else:
        out["mid"] = pd.NA
        out["spread_pct"] = pd.NA

    # preço utilizável: mid do book, senão fechamento, senão preço de referência
    out["price"] = out["mid"]
    for fallback in ("close", "option_price", "reference_price"):
        if fallback in out.columns:
            out["price"] = out["price"].fillna(out[fallback])

    if spot:
        out["spot"] = spot
        out["moneyness"] = out["strike"] / spot
    else:
        out["spot"] = pd.NA
        out["moneyness"] = pd.NA

    exp = pd.to_datetime(out["expiration"], errors="coerce")
    td = pd.to_datetime(out["trade_date"], errors="coerce")
    out["dte"] = (exp - td).dt.days
    return out



FAIXA_PADRAO = 0.10       # ±10% em torno do preço do ativo


def terceira_sexta(ano: int, mes: int) -> date:
    """
    Vencimento mensal padrão da B3: terceira sexta-feira do mês.

    É o que separa série mensal de semanal. As semanais vencem nas demais
    sextas e têm liquidez bem menor — misturar as duas na mesma tabela
    atrapalha justamente a decisão rápida que esta ferramenta serve.
    """
    sextas = [d for d in range(1, calendar.monthrange(ano, mes)[1] + 1)
              if date(ano, mes, d).weekday() == 4]
    return date(ano, mes, sextas[2])


def vencimentos_mensais(vencs: list[str], hoje: date, quantos: int = 2) -> list[str]:
    """
    Os próximos vencimentos mensais ainda abertos.

    A referência é a terceira sexta de cada mês, mas a busca é tolerante:
    quando ela cai em feriado a B3 desloca o vencimento para um dia útil
    próximo. Exigir a data exata pulava o mês inteiro em silêncio — foi o
    que aconteceu com 20/11/2026, Dia da Consciência Negra. Agora cada mês
    é resolvido individualmente, aceitando até 4 dias de deslocamento.
    """
    futuros = sorted(v for v in vencs if v >= hoje.isoformat())
    saida: list[str] = []
    ano, mes = hoje.year, hoje.month
    for _ in range(quantos + 4):
        alvo = terceira_sexta(ano, mes)
        perto = [v for v in futuros
                 if abs((date.fromisoformat(v) - alvo).days) <= 4]
        if perto:
            escolhido = min(perto, key=lambda v: abs((date.fromisoformat(v) - alvo).days))
            if escolhido != alvo.isoformat():
                log.info("Vencimento de %02d/%d deslocado de %s para %s "
                         "(provável feriado)", mes, ano, alvo, escolhido)
            if escolhido not in saida:
                saida.append(escolhido)
        if len(saida) == quantos:
            break
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return saida

def spot_da_cotacao(client: BrapiClient, ativo: str) -> tuple[float | None, str]:
    """
    Preço do ATIVO pelo endpoint de cotação, que atualiza durante o pregão.

    Separado de propósito do preço das opções: a cotação tem atraso de
    minutos, enquanto a cadeia de opções é do último pregão fechado. Usar a
    cotação só para centrar a faixa e medir distância mantém o recorte
    alinhado ao mercado de agora, sem fingir que os prêmios são de agora.
    """
    try:
        payload = client._get(QUOTE_URL.format(ticker=ativo))
    except BrapiError as exc:
        log.warning("Cotação de %s indisponível (%s)", ativo, str(exc)[:70])
        return None, ""

    for rec in _records(payload):
        preco = None
        for campo in ("regularMarketPrice", "price", "close"):
            if rec.get(campo) is not None:
                preco = float(rec[campo])
                break
        if preco is None:
            continue
        bruto = (rec.get("regularMarketTime") or rec.get("updatedAt")
                 or rec.get("date"))
        return preco, _hora_brasilia(bruto)
    return None, ""


def _hora_brasilia(valor: Any) -> str:
    """
    Converte o horário da API para Brasília.

    A brapi informa o horário da cotação em UTC. Exibido cru, 12h15 de
    Brasília aparecia como 15h15 — três horas à frente, o que faz uma
    cotação atual parecer futura, ou uma defasada parecer recente.
    Aceita texto ISO (com ou sem fuso) e epoch em segundos.
    """
    if valor is None or valor == "":
        return ""
    try:
        if isinstance(valor, (int, float)):
            ts = pd.Timestamp(valor, unit="s", tz="UTC")
        else:
            ts = pd.Timestamp(str(valor))
            if ts.tzinfo is None:          # sem fuso explícito: a API usa UTC
                ts = ts.tz_localize("UTC")
        return ts.tz_convert("America/Sao_Paulo").strftime("%d/%m/%Y %H:%M:%S")
    except (ValueError, TypeError):
        return str(valor)[:19]


def variacao(linha: pd.Series) -> float | None:
    """Variação do último pregão. Usa o campo da API; se faltar, calcula."""
    for campo in ("changePercent", "change_percent", "variation"):
        v = linha.get(campo)
        if v is not None and not pd.isna(v):
            return float(v) / 100 if abs(float(v)) > 1.5 else float(v)
    fech, ant = linha.get("close"), linha.get("previousClose")
    if fech and ant and not pd.isna(fech) and not pd.isna(ant) and float(ant) > 0:
        return float(fech) / float(ant) - 1
    abertura = linha.get("open")
    if fech and abertura and not pd.isna(abertura) and float(abertura) > 0:
        return float(fech) / float(abertura) - 1
    return None


def montar_tabela(df: pd.DataFrame, spot: float, faixa: float) -> pd.DataFrame:
    """Recorta a faixa em torno do dinheiro e ordena por strike."""
    if df.empty:
        return df
    out = df.dropna(subset=["strike"]).copy()
    lo, hi = spot * (1 - faixa), spot * (1 + faixa)
    out = out[(out["strike"] >= lo) & (out["strike"] <= hi)]
    out["var"] = out.apply(variacao, axis=1)
    out["dist"] = (out["strike"] / spot - 1)
    ordem = ["symbol", "side", "strike", "dist", "price", "var", "volume",
             "trades", "open_interest", "optionStyle", "expiration", "dte"]
    presentes = [c for c in ordem if c in out.columns]
    return out.sort_values(["expiration", "side", "strike"])[presentes]


def imprimir(tab: pd.DataFrame, spot: float, ativo: str) -> None:
    if tab.empty:
        print("Nenhuma série na faixa.")
        return
    for exp, g in tab.groupby("expiration"):
        d = g["dte"].dropna() if "dte" in g else pd.Series(dtype=float)
        dias = int(d.iloc[0]) if not d.empty else 0
        print(f"\n=== {ativo} · vencimento {str(exp)[:10]} ({dias} dias) · "
              f"spot {spot:.2f} ===")
        for lado in ("call", "put"):
            sub = g[g["side"] == lado]
            if sub.empty:
                continue
            print(f"\n  {lado.upper()}S")
            print(f"  {'símbolo':<12}{'strike':>8}{'dist':>8}{'preço':>8}"
                  f"{'var':>8}{'volume':>10}{'negóc':>8}{'aberto':>9}  estilo")
            for _, r in sub.iterrows():
                def n(v, f="{:.2f}"):
                    return "—" if v is None or pd.isna(v) else f.format(v)
                print(f"  {str(r.get('symbol','')):<12}"
                      f"{n(r.get('strike')):>8}"
                      f"{n(r.get('dist'), '{:+.1%}'):>8}"
                      f"{n(r.get('price')):>8}"
                      f"{n(r.get('var'), '{:+.1%}'):>8}"
                      f"{n(r.get('volume'), '{:,.0f}'):>10}"
                      f"{n(r.get('trades'), '{:,.0f}'):>8}"
                      f"{n(r.get('open_interest'), '{:,.0f}'):>9}"
                      f"  {str(r.get('optionStyle',''))[:9]}")


def _deriva_html(spot: float, fechamento: float | None) -> str:
    """Mostra o quanto o ativo andou desde o fechamento das opções."""
    if not fechamento:
        return ""
    d = spot / fechamento - 1
    if abs(d) < 0.005:
        return ""
    classe = "alerta" if abs(d) > 0.03 else "nota"
    return (f'<p class="{classe}">O ativo está {d:+.2%} em relação ao '
            f'fechamento de {fechamento:.2f}, quando os prêmios abaixo foram '
            f'apurados.</p>')


def gerar_html(tab: pd.DataFrame, spot: float, ativo: str, quando: str,
               origem_spot: str = "", spot_fechamento: float | None = None) -> str:
    """Tabela para ler no celular: uma linha por série, calls e puts separadas."""
    def cel(v, f="{:.2f}", classe=""):
        if v is None or pd.isna(v):
            return '<td class="nd">—</td>'
        return f'<td class="{classe}">{f.format(v)}</td>'

    blocos = []
    for exp, g in tab.groupby("expiration"):
        d = g["dte"].dropna() if "dte" in g else pd.Series(dtype=float)
        dias = int(d.iloc[0]) if not d.empty else 0
        linhas = []
        for lado in ("call", "put"):
            sub = g[g["side"] == lado]
            if sub.empty:
                continue
            linhas.append(f'<tr class="sep"><th colspan="9">{lado.upper()}S</th></tr>')
            for _, r in sub.iterrows():
                v = r.get("var")
                cls = "" if v is None or pd.isna(v) else ("alta" if v > 0 else "baixa")
                no_dinheiro = abs(float(r.get("dist", 1))) < 0.02
                linhas.append(
                    f'<tr class="{"atm" if no_dinheiro else ""}">'
                    f'<td class="sym">{_html.escape(str(r.get("symbol","")))}</td>'
                    + cel(r.get("strike")) + cel(r.get("dist"), "{:+.1%}")
                    + cel(r.get("price")) + cel(r.get("var"), "{:+.1%}", cls)
                    + cel(r.get("volume"), "{:,.0f}") + cel(r.get("trades"), "{:,.0f}")
                    + cel(r.get("open_interest"), "{:,.0f}")
                    + f'<td class="est">{_html.escape(str(r.get("optionStyle",""))[:3])}</td>'
                    "</tr>")
        blocos.append(f"""
        <section>
          <h2>Vencimento {str(exp)[:10]} · {dias} dias</h2>
          <table><thead><tr><th>série</th><th>strike</th><th>dist</th>
            <th>preço</th><th>var</th><th>volume</th><th>neg.</th>
            <th>aberto</th><th>est.</th></tr></thead>
          <tbody>{"".join(linhas)}</tbody></table>
        </section>""")

    return f"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html.escape(ativo)} · opções no dinheiro</title><style>
:root{{color-scheme:light}}
body{{margin:0;background:#EDF0F3;color:#14263D;
 font-family:system-ui,-apple-system,"Segoe UI",sans-serif;font-size:14px}}
.folha{{max-width:840px;margin:0 auto;padding:20px 12px 48px}}
h1{{font-size:1.25rem;margin:0 0 2px}}
.spot{{font-size:2.2rem;font-variant-numeric:tabular-nums;margin:6px 0 0}}
.origem{{color:#5A6B82;font-size:.85rem;margin:2px 0 20px}}
h2{{font-size:.95rem;margin:26px 0 6px;border-bottom:1px solid #14263D;
 padding-bottom:5px}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
th{{color:#5A6B82;font-weight:600;text-align:right;padding:5px 4px;
 font-size:.78rem}}
td{{text-align:right;padding:5px 4px;border-bottom:1px solid #D6DDE4}}
.sym,td.sym{{text-align:left;font-size:.8rem}}
tr.sep th{{text-align:left;color:#14263D;padding-top:12px;font-size:.8rem;
 border-bottom:1px solid #C9D2DC}}
tr.atm{{background:#DCE6F2}}
.alta{{color:#0F6E52}} .baixa{{color:#9A2F2F}} .nd{{color:#9AA7B5}}
.est{{color:#5A6B82;font-size:.75rem}}
.aviso{{background:#E3E9EF;border-left:3px solid #5A6B82;padding:10px 12px;
 margin:20px 0 0;color:#3A4A5E;font-size:.85rem}}
.nota{{color:#5A6B82;font-size:.85rem;margin:0 0 14px}}
.alerta{{background:#F4E3E3;border-left:3px solid #9A2F2F;padding:8px 10px;
 margin:0 0 14px;color:#7A2424;font-size:.85rem}}
</style></head><body><div class="folha">
<h1>{_html.escape(ativo)} · opções em torno do dinheiro</h1>
<p class="spot">{spot:.2f}</p>
<p class="origem">{_html.escape(origem_spot) or quando} · linha destacada =
strike a menos de 2% do preço</p>
{_deriva_html(spot, spot_fechamento)}
{"".join(blocos) or '<p>Nenhuma série na faixa.</p>'}
<p class="aviso"><strong>Duas origens de preço nesta tabela.</strong>
O valor grande acima é a cotação do ativo, que atualiza durante o pregão com
alguns minutos de atraso — é ela que define a faixa e a coluna de distância.
Já preço, variação, volume e negócios das séries são do <strong>último pregão
fechado</strong>. Confira o prêmio na corretora antes de enviar ordem.</p>
</div></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Opções no dinheiro, mês atual e próximo")
    ap.add_argument("--underlying", default="PETR4")
    ap.add_argument("--faixa", type=float, default=FAIXA_PADRAO,
                    help="largura em torno do preço (0.10 = ±10%%)")
    ap.add_argument("--estilo", choices=["todos", "american", "european"],
                    default="todos",
                    help="na B3 calls são americanas e puts europeias; "
                         "filtrar por estilo costuma eliminar um dos lados")
    ap.add_argument("--min-negocios", type=int, default=0,
                    help="descarta séries com menos negócios que isto")
    ap.add_argument("--html", type=Path, help="também grava uma tabela HTML")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    client = BrapiClient(token=os.environ.get("BRAPI_TOKEN"))
    ativo = args.underlying.upper()

    hoje = date.today()
    vencs = parse_expirations(client.expirations(ativo))
    alvos = vencimentos_mensais(vencs, hoje)
    if not alvos:
        raise SystemExit(f"Nenhum vencimento mensal encontrado para {ativo}")
    log.info("Vencimentos mensais: %s", ", ".join(alvos))

    # A cotação vem ANTES das séries: é ela que define a faixa a recortar.
    spot, hora_spot = spot_da_cotacao(client, ativo)
    origem_spot = f"cotação{f' de {hora_spot}' if hora_spot else ''}"
    if spot:
        log.info("Spot %s = %.2f (%s)", ativo, spot, origem_spot)

    partes, spot_fechamento = [], None
    for exp in alvos:
        payload = client.chain(ativo, exp)
        df = normalize(payload, underlying=ativo, expiration=exp,
                       trade_date=hoje.isoformat())
        if df.empty:
            log.warning("Vencimento %s sem séries", exp)
            continue
        if spot_fechamento is None and "spot_payload" in df.columns:
            v = pd.to_numeric(df["spot_payload"], errors="coerce").dropna()
            spot_fechamento = float(v.iloc[0]) if not v.empty else None
        partes.append(df)

    if not partes:
        raise SystemExit("Nenhuma série retornada")
    if spot is None:
        spot, origem_spot = spot_fechamento, "fechamento do último pregão"
        log.warning("Usando o preço de fechamento como referência — a faixa "
                    "e a distância podem sair deslocadas do mercado de agora")
    elif spot_fechamento:
        deriva = spot / spot_fechamento - 1
        log.info("Ativo variou %+.2f%% desde o fechamento das opções", deriva * 100)
        if abs(deriva) > 0.03:
            log.warning("Desvio acima de 3%%: os prêmios do último pregão "
                        "estão defasados em relação ao preço atual")
    if not spot:
        raise SystemExit("Não obtive o preço do ativo — sem ele não há "
                         "'linha do dinheiro' para calcular")
    log.info("Spot %s = %.2f", ativo, spot)

    df = enrich(pd.concat(partes, ignore_index=True), spot)
    # Para decidir entrada, o prazo que importa é de HOJE até o vencimento —
    # e não depende da data do pregão, cujo formato varia entre endpoints.
    venc = pd.to_datetime(df["expiration"], errors="coerce", format="mixed")
    df["dte"] = (venc - pd.Timestamp(hoje)).dt.days
    if args.estilo != "todos" and "optionStyle" in df.columns:
        antes = len(df)
        df = df[df["optionStyle"].astype(str).str.lower() == args.estilo]
        log.info("Filtro de estilo %s: %d -> %d séries", args.estilo, antes, len(df))
    if args.min_negocios and "trades" in df.columns:
        df = df[pd.to_numeric(df["trades"], errors="coerce").fillna(0)
                >= args.min_negocios]

    tab = montar_tabela(df, spot, args.faixa)
    log.info("%d séries na faixa de ±%.0f%%", len(tab), args.faixa * 100)
    imprimir(tab, spot, ativo)

    if args.html:
        quando = datetime.now().strftime("consultado em %d/%m/%Y às %H:%M")
        args.html.write_text(
            gerar_html(tab, spot, ativo, quando, origem_spot, spot_fechamento),
            encoding="utf-8")
        log.info("HTML em %s", args.html)


if __name__ == "__main__":
    main()
