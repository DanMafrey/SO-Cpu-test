#!/usr/bin/env python3
"""
medir.py - Mede quanto o tempo de resposta de um "mini-jogo" piora
quando outros programas disputam a CPU.

O que o script faz, para cada nivel de carga (0, 25, 50, 75, 100 %):
  1. Liga processos que ocupam a CPU no nivel pedido (a "disputa").
  2. Roda um mini-jogo a 60 quadros/s, com trabalho fixo por quadro,
     e mede o tempo de cada quadro e o tempo entre uma "entrada" simulada
     (um clique) e o quadro que a processa.
  3. Mede o uso REAL de CPU durante o teste (nao o configurado).
  4. Salva tudo em CSV (dados brutos + resumo) e gera um grafico.

Requisitos: Python 3.11+ e psutil  (pip install psutil)
Opcional:   matplotlib para o grafico  (pip install matplotlib)

Uso rapido (teste de uns 3 minutos):
    python medir.py --niveis 0 50 100 --reps 1 --duracao 20 --aquecimento 3

Uso completo (uns 10 minutos):
    python medir.py
"""
import argparse
import csv
import math
import multiprocessing as mp
import os
import random
import statistics
import sys
import time
from collections import deque

try:
    import psutil
except ImportError:
    sys.exit("Falta o psutil. Instale com:  pip install psutil")

FPS_ALVO = 60
ORCAMENTO = 1 / FPS_ALVO          # tempo disponivel por quadro (16,7 ms)
SEMENTE_ENTRADAS = 123            # mesmas "entradas" em todos os testes


# ----------------------------------------------------------------- utilidades
def percentil(valores, p):
    """Percentil p (0-100) com interpolacao linear."""
    if not valores:
        return float("nan")
    v = sorted(valores)
    k = (len(v) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    return v[f] + (v[c] - v[f]) * (k - f)


# ------------------------------------------------------- carga (a "disputa")
def carga(nivel, nucleo, parar):
    """Processo que ocupa `nivel` % de um nucleo (ciclos de 100 ms)."""
    try:
        psutil.Process().cpu_affinity([nucleo])
    except Exception:
        pass
    periodo = 0.1
    while not parar.is_set():
        t = time.perf_counter()
        ocupado = periodo if nivel >= 100 else periodo * nivel / 100
        while time.perf_counter() - t < ocupado:
            pass
        if nivel < 100:
            time.sleep(periodo - ocupado)


# --------------------------------------------------------------- mini-jogo
def trabalho(n):
    """Trabalho fixo de cada quadro (fisica/IA de mentira)."""
    x = 0
    for i in range(n):
        x += i * i % 7
    return x


def calibrar(alvo=0.004):
    """Acha o n que faz o trabalho durar ~4 ms com a CPU livre."""
    n = 20000
    for _ in range(6):
        t = time.perf_counter()
        trabalho(n)
        dt = time.perf_counter() - t
        n = max(1000, int(n * alvo / dt))
    amostras = []
    for _ in range(15):
        t = time.perf_counter()
        trabalho(n)
        amostras.append(time.perf_counter() - t)
    return n, statistics.median(amostras) * 1000


def rodar_jogo(n_trabalho, segundos):
    """
    Roda o mini-jogo por `segundos`.
    Devolve (quadros, respostas):
      quadros   = [(instante_s, intervalo_ms, trabalho_ms), ...]
      respostas = [(instante_s, resposta_ms), ...]
    """
    rng = random.Random(SEMENTE_ENTRADAS)
    entradas, t = [], 0.0
    while t < segundos:
        t += rng.expovariate(10)      # uma entrada a cada ~100 ms
        entradas.append(t)
    entradas = deque(entradas)

    quadros, respostas = [], []
    inicio = time.perf_counter()
    proximo = inicio
    anterior = None
    while True:
        ini = time.perf_counter()
        rel = ini - inicio
        if rel >= segundos:
            break
        intervalo = None if anterior is None else (ini - anterior) * 1000
        anterior = ini

        pendentes = []
        while entradas and entradas[0] <= rel:
            pendentes.append(entradas.popleft())

        trabalho(n_trabalho)
        fim = time.perf_counter()

        for e in pendentes:
            respostas.append((e, (fim - inicio - e) * 1000))
        if intervalo is not None:
            quadros.append((rel, intervalo, (fim - ini) * 1000))

        # espera ate o proximo quadro (dorme, e so "gira" no final)
        proximo += ORCAMENTO
        agora = time.perf_counter()
        if agora > proximo + ORCAMENTO:   # atrasou demais: ressincroniza
            proximo = agora
        while True:
            falta = proximo - time.perf_counter()
            if falta <= 0:
                break
            if falta > 0.002:
                time.sleep(0.001)
    return quadros, respostas


# ------------------------------------------------------------ um teste so
def rodar_teste(nivel, args, n_trabalho, nucleos):
    parar = mp.Event()
    procs = []
    if nivel > 0:
        for c in nucleos:
            p = mp.Process(target=carga, args=(nivel, c, parar), daemon=True)
            p.start()
            procs.append(p)
    time.sleep(0.5)                        # deixa a carga estabilizar

    rodar_jogo(n_trabalho, args.aquecimento)   # aquecimento (descartado)
    psutil.cpu_percent(percpu=True)            # zera o contador de CPU
    quadros, respostas = rodar_jogo(n_trabalho, args.duracao)
    uso = psutil.cpu_percent(percpu=True)

    parar.set()
    for p in procs:
        p.join(timeout=2)
        if p.is_alive():
            p.terminate()

    uso_nucleos = [uso[c] for c in nucleos if c < len(uso)]
    return quadros, respostas, statistics.mean(uso_nucleos)


def resumir(nivel, rep, quadros, respostas, uso):
    iv = [q[1] for q in quadros]
    tr = [q[2] for q in quadros]
    rp = [r[1] for r in respostas]
    if not iv or not rp:
        return None
    perdidos = sum(1 for x in iv if x > 1.5 * ORCAMENTO * 1000)
    return {
        "nivel_carga": nivel,
        "repeticao": rep,
        "cpu_real_pct": round(uso, 1),
        "quadros": len(iv),
        "fps_medio": round(1000 / statistics.mean(iv), 1),
        "fps_pior_1pct": round(1000 / percentil(iv, 99), 1),
        "quadro_mediana_ms": round(percentil(iv, 50), 2),
        "quadro_p95_ms": round(percentil(iv, 95), 2),
        "quadro_p99_ms": round(percentil(iv, 99), 2),
        "quadro_max_ms": round(max(iv), 2),
        "quadros_perdidos_pct": round(100 * perdidos / len(iv), 2),
        "trabalho_mediana_ms": round(percentil(tr, 50), 2),
        "trabalho_p95_ms": round(percentil(tr, 95), 2),
        "resposta_mediana_ms": round(percentil(rp, 50), 2),
        "resposta_p95_ms": round(percentil(rp, 95), 2),
        "resposta_p99_ms": round(percentil(rp, 99), 2),
        "resposta_max_ms": round(max(rp), 2),
    }


# ------------------------------------------------------------------ saida
def salvar_csv(caminho, linhas, campos=None):
    if not linhas:
        return
    campos = campos or list(linhas[0].keys())
    with open(caminho, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        w.writerows(linhas)


def agregar(linhas):
    campos = [k for k in linhas[0] if k not in ("nivel_carga", "repeticao")]
    saida = []
    for nivel in sorted({l["nivel_carga"] for l in linhas}):
        grupo = [l for l in linhas if l["nivel_carga"] == nivel]
        agg = {"nivel_carga": nivel, "repeticoes": len(grupo)}
        for c in campos:
            agg[c] = round(statistics.median(l[c] for l in grupo), 2)
        saida.append(agg)
    return saida


def grafico(agg, caminho):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(matplotlib nao instalado: grafico nao gerado)")
        return
    x = [a["nivel_carga"] for a in agg]
    fig, eixos = plt.subplots(1, 3, figsize=(15, 4))
    eixos[0].plot(x, [a["quadro_p95_ms"] for a in agg], "o-", label="p95")
    eixos[0].plot(x, [a["quadro_p99_ms"] for a in agg], "s-", label="p99")
    eixos[0].axhline(ORCAMENTO * 1000, ls="--", c="gray", label="limite 60 FPS")
    eixos[0].set_title("Tempo de quadro")
    eixos[0].set_ylabel("ms")
    eixos[1].plot(x, [a["resposta_p95_ms"] for a in agg], "o-", label="p95")
    eixos[1].plot(x, [a["resposta_mediana_ms"] for a in agg], "s-", label="mediana")
    eixos[1].set_title("Tempo entrada -> resposta")
    eixos[1].set_ylabel("ms")
    eixos[2].plot(x, [a["quadros_perdidos_pct"] for a in agg], "o-")
    eixos[2].set_title("Quadros perdidos")
    eixos[2].set_ylabel("% dos quadros")
    for e in eixos:
        e.set_xlabel("Carga de CPU gerada (%)")
        e.grid(alpha=0.3)
        if e.get_legend_handles_labels()[0]:
            e.legend()
    fig.tight_layout()
    fig.savefig(caminho, dpi=150)
    print(f"Grafico salvo em {caminho}")


# ------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--niveis", type=int, nargs="+", default=[0, 25, 50, 75, 100],
                    help="niveis de carga de CPU em %% (padrao: 0 25 50 75 100)")
    ap.add_argument("--reps", type=int, default=3, help="repeticoes por nivel")
    ap.add_argument("--duracao", type=float, default=30, help="segundos medidos por teste")
    ap.add_argument("--aquecimento", type=float, default=5, help="segundos descartados no inicio")
    ap.add_argument("--nucleos", type=int, default=2,
                    help="quantos nucleos o jogo e a carga vao dividir")
    ap.add_argument("--trabalho-ms", type=float, default=4,
                    help="trabalho do mini-jogo por quadro, em ms (padrao 4; "
                         "valores maiores, como 10, deixam o efeito da disputa mais visivel)")
    ap.add_argument("--saida", default="resultados", help="pasta de saida")
    args = ap.parse_args()

    os.makedirs(args.saida, exist_ok=True)
    nucleos = list(range(min(args.nucleos, psutil.cpu_count(logical=True))))
    try:
        psutil.Process().cpu_affinity(nucleos)
    except Exception:
        print("AVISO: este sistema nao permite fixar nucleos; a disputa pode ser menor.")

    n_trabalho, t_trab = calibrar(args.trabalho_ms / 1000)
    print(f"Nucleos usados: {nucleos} | trabalho por quadro: ~{t_trab:.1f} ms "
          f"(limite {ORCAMENTO * 1000:.1f} ms)")
    print("Feche outros programas e deixe o computador na tomada (alto desempenho).\n")

    plano = [(n, r) for n in args.niveis for r in range(1, args.reps + 1)]
    random.Random(42).shuffle(plano)           # ordem embaralhada
    tempo_total = len(plano) * (args.aquecimento + args.duracao + 1.5) / 60
    print(f"{len(plano)} testes, cerca de {tempo_total:.0f} min.\n")

    linhas, brutos_q, brutos_r = [], [], []
    for i, (nivel, rep) in enumerate(plano, 1):
        quadros, respostas, uso = rodar_teste(nivel, args, n_trabalho, nucleos)
        res = resumir(nivel, rep, quadros, respostas, uso)
        if res is None:
            print(f"[{i}/{len(plano)}] carga {nivel}% rep {rep}: sem dados, ignorado")
            continue
        linhas.append(res)
        brutos_q += [{"nivel_carga": nivel, "repeticao": rep, "t_s": round(a, 4),
                      "intervalo_ms": round(b, 3), "trabalho_ms": round(c, 3)}
                     for a, b, c in quadros]
        brutos_r += [{"nivel_carga": nivel, "repeticao": rep, "t_s": round(a, 4),
                      "resposta_ms": round(b, 3)} for a, b in respostas]
        print(f"[{i}/{len(plano)}] carga {nivel:>3}% rep {rep} | CPU real {uso:5.1f}% | "
              f"quadro p95 {res['quadro_p95_ms']:6.2f} ms | "
              f"resposta p95 {res['resposta_p95_ms']:6.2f} ms | "
              f"perdidos {res['quadros_perdidos_pct']:5.2f}%")
        # salva a cada teste, para nao perder nada se interromper
        salvar_csv(os.path.join(args.saida, "resumo_por_teste.csv"), linhas)

    if not linhas:
        sys.exit("Nenhum dado coletado.")
    salvar_csv(os.path.join(args.saida, "quadros_brutos.csv"), brutos_q)
    salvar_csv(os.path.join(args.saida, "respostas_brutas.csv"), brutos_r)
    agg = agregar(linhas)
    salvar_csv(os.path.join(args.saida, "resumo_por_nivel.csv"), agg)
    grafico(agg, os.path.join(args.saida, "grafico.png"))

    print("\nMediana das repeticoes por nivel de carga:")
    print(f"{'carga':>6} {'CPU real':>9} {'FPS':>6} {'FPS 1%':>7} {'quadro p95':>11} "
          f"{'quadro p99':>11} {'perdidos':>9} {'resp. p95':>10}")
    for a in agg:
        print(f"{a['nivel_carga']:>5}% {a['cpu_real_pct']:>8.1f}% {a['fps_medio']:>6.1f} "
              f"{a['fps_pior_1pct']:>7.1f} {a['quadro_p95_ms']:>9.2f}ms "
              f"{a['quadro_p99_ms']:>9.2f}ms {a['quadros_perdidos_pct']:>8.2f}% "
              f"{a['resposta_p95_ms']:>8.2f}ms")
    print(f"\nArquivos em: {os.path.abspath(args.saida)}")


if __name__ == "__main__":
    mp.freeze_support()
    main()
