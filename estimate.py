"""Compare fictional VM schedules against an offline Azure retail snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
API = "https://prices.azure.com/api/retail/prices"
LIMIT = Decimal("1000000")


class ValidationError(ValueError):
    """The input does not match this toolkit's explicit calculation scope."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"Campo JSON duplicado: {key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValidationError(f"Número JSON não finito: {value}")


def read_json(path: Path) -> Any:
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_float=Decimal,
        parse_constant=_constant,
        object_pairs_hook=_pairs,
    )


def _object(value: Any, keys: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{where}: esperado objeto JSON")
    if set(value) != keys:
        missing = sorted(keys - set(value))
        extra = sorted(set(value) - keys)
        raise ValidationError(f"{where}: campos ausentes={missing}, desconhecidos={extra}")
    return value


def _text(value: Any, where: str, limit: int = 140) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValidationError(f"{where}: texto obrigatório, máximo {limit} caracteres")
    if any(ord(char) < 32 for char in value):
        raise ValidationError(f"{where}: caracteres de controle não são permitidos")
    return value


def _version(value: Any) -> None:
    if type(value) is not int or value != 1:
        raise ValidationError("schema_version: esperado inteiro 1")


def _number(value: Any, where: str, *, integer: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValidationError(f"{where}: esperado número JSON, sem booleanos ou texto")
    number = Decimal(value)
    if not number.is_finite() or number < 0 or number > LIMIT:
        raise ValidationError(f"{where}: esperado número finito entre 0 e 1000000")
    if number.as_tuple().exponent < -8:
        raise ValidationError(f"{where}: máximo de 8 casas decimais")
    if integer and number != number.to_integral_value():
        raise ValidationError(f"{where}: quantidade precisa ser inteira")
    return number


def _catalog_number(value: Any, where: str) -> Decimal:
    # Snapshot prices use strings to preserve the decimal supplied by the API.
    if isinstance(value, str):
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            raise ValidationError(f"{where}: decimal inválido")
        value = Decimal(value)
    return _number(value, where)


def _utc(value: Any, where: str) -> str:
    text = _text(value, where)
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{where}: data ISO 8601 inválida") from exc
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise ValidationError(f"{where}: data precisa ter fuso UTC explícito")
    return text


@dataclass(frozen=True)
class Catalog:
    currency: str
    region: str
    consulted_at: str
    kind: str
    source_url: str
    prices: dict[str, Decimal]


def validate_catalog(data: Any) -> Catalog:
    data = _object(data, {
        "schema_version", "kind", "currencyCode", "armRegionName", "consulted_at",
        "source", "selection_criteria", "items",
    }, "catálogo")
    _version(data["schema_version"])
    if data["kind"] not in ("public_price_snapshot", "simulation"):
        raise ValidationError("catálogo.kind: snapshot público ou simulation obrigatório")
    if data["currencyCode"] != "USD" or data["armRegionName"] != "brazilsouth":
        raise ValidationError("catálogo: somente USD e brazilsouth são suportados")
    consulted = _utc(data["consulted_at"], "catálogo.consulted_at")
    source = _object(data["source"], {"api", "query_url", "http_status"}, "fonte")
    if source["api"] != API:
        raise ValidationError("fonte.api: esperado endpoint público Azure Retail Prices")
    query = _text(source["query_url"], "fonte.query_url", 2000)
    if data["kind"] == "public_price_snapshot":
        if not query.startswith(API + "?") or type(source["http_status"]) is not int or source["http_status"] != 200:
            raise ValidationError("snapshot público requer query da API e HTTP 200 registrado")
    elif source["http_status"] is not None:
        raise ValidationError("simulation requer http_status null, sem alegar consulta válida")
    criteria = data["selection_criteria"]
    if not isinstance(criteria, list) or not criteria:
        raise ValidationError("selection_criteria: lista não vazia obrigatória")
    for index, criterion in enumerate(criteria):
        _text(criterion, f"selection_criteria[{index}]", 500)
    items = data["items"]
    if not isinstance(items, list) or not items or len(items) > 100:
        raise ValidationError("catálogo.items: esperado entre 1 e 100 preços")
    prices: dict[str, Decimal] = {}
    for index, item in enumerate(items):
        at = f"items[{index}]"
        item = _object(item, {
            "armSkuName", "productName", "meterName", "skuName", "unitOfMeasure",
            "currencyCode", "retailPrice", "effectiveStartDate", "armRegionName",
            "type", "tierMinimumUnits", "source", "consulted_at",
        }, at)
        sku = _text(item["armSkuName"], at + ".armSkuName")
        product = _text(item["productName"], at + ".productName")
        meter = _text(item["meterName"], at + ".meterName")
        sku_name = _text(item["skuName"], at + ".skuName")
        compact = re.sub(r"[\s_-]", "", f"{product} {meter} {sku_name}".lower())
        if any(word in compact for word in ("windows", "spot", "lowpriority", "reservation")):
            raise ValidationError(f"{at}: Windows, Spot, Low Priority e Reservation excluídos")
        if item["type"] != "Consumption":
            raise ValidationError(f"{at}: somente Consumption regular é suportado")
        if not sku.startswith("Standard_") or meter != sku_name or sku_name != sku.removeprefix("Standard_"):
            raise ValidationError(f"{at}: SKU e medidor precisam representar VM regular")
        if item["currencyCode"] != "USD" or item["armRegionName"] != "brazilsouth":
            raise ValidationError(f"{at}: moeda ou região incompatível com o catálogo")
        if item["unitOfMeasure"] != "1 Hour":
            raise ValidationError(f"{at}: unidade desconhecida; esperado 1 Hour")
        if _catalog_number(item["tierMinimumUnits"], at + ".tierMinimumUnits") != 0:
            raise ValidationError(f"{at}: tarifas em camadas não são suportadas")
        _utc(item["effectiveStartDate"], at + ".effectiveStartDate")
        if item["consulted_at"] != consulted or item["source"] != query:
            raise ValidationError(f"{at}: proveniência deve corresponder ao snapshot")
        if sku in prices:
            raise ValidationError(f"{at}: preço duplicado para {sku}")
        prices[sku] = _catalog_number(item["retailPrice"], at + ".retailPrice")
    return Catalog("USD", "brazilsouth", consulted, data["kind"], query, prices)


@dataclass(frozen=True)
class Resource:
    name: str
    sku: str
    quantity: int
    hours: Decimal


@dataclass(frozen=True)
class Scenario:
    title: str
    period_hours: Decimal
    base_label: str
    base: tuple[Resource, ...]
    optimized_label: str
    optimized: tuple[Resource, ...]


def _side(value: Any, catalog: Catalog, period: Decimal, where: str) -> tuple[str, tuple[Resource, ...]]:
    value = _object(value, {"label", "resources"}, where)
    label = _text(value["label"], where + ".label")
    rows = value["resources"]
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValidationError(f"{where}.resources: esperado lista de até 1000 recursos")
    resources: list[Resource] = []
    names: set[str] = set()
    for index, row in enumerate(rows):
        at = f"{where}.resources[{index}]"
        row = _object(row, {"name", "armSkuName", "quantity", "hours"}, at)
        name = _text(row["name"], at + ".name")
        if name in names:
            raise ValidationError(f"{at}: nome de recurso duplicado")
        names.add(name)
        sku = _text(row["armSkuName"], at + ".armSkuName")
        if sku not in catalog.prices:
            raise ValidationError(f"{at}: SKU sem preço no catálogo: {sku}")
        quantity = int(_number(row["quantity"], at + ".quantity", integer=True))
        hours = _number(row["hours"], at + ".hours")
        if hours > period:
            raise ValidationError(f"{at}: hours não pode exceder period_hours")
        resources.append(Resource(name, sku, quantity, hours))
    return label, tuple(resources)


def validate_scenario(data: Any, catalog: Catalog) -> Scenario:
    data = _object(data, {
        "schema_version", "title", "period_hours", "currencyCode", "armRegionName",
        "base", "optimized",
    }, "cenário")
    _version(data["schema_version"])
    if data["currencyCode"] != catalog.currency or data["armRegionName"] != catalog.region:
        raise ValidationError("cenário: moeda e região precisam corresponder ao catálogo")
    title = _text(data["title"], "title")
    period = _number(data["period_hours"], "period_hours")
    if period == 0:
        raise ValidationError("period_hours: precisa ser maior que zero")
    base_label, base = _side(data["base"], catalog, period, "base")
    proposed_label, proposed = _side(data["optimized"], catalog, period, "optimized")
    return Scenario(title, period, base_label, base, proposed_label, proposed)


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _cost(resources: tuple[Resource, ...], catalog: Catalog) -> tuple[Decimal, list[dict[str, Any]]]:
    total = Decimal("0")
    rows = []
    for resource in resources:
        rate = catalog.prices[resource.sku]
        cost = rate * resource.hours * resource.quantity
        total += cost
        rows.append({
            "name": resource.name, "armSkuName": resource.sku,
            "quantity": resource.quantity, "hours": decimal_text(resource.hours),
            "usd_per_hour": decimal_text(rate), "compute_usd": decimal_text(cost),
        })
    return total, rows


def compare(catalog: Catalog, scenario: Scenario) -> dict[str, Any]:
    # Bounds in validation keep all multiplications exact within this precision.
    with localcontext() as context:
        context.prec = 64
        base, base_rows = _cost(scenario.base, catalog)
        proposed, proposed_rows = _cost(scenario.optimized, catalog)
        savings = base - proposed
        percent = None if base == 0 else (savings / base * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "title": scenario.title, "currencyCode": catalog.currency,
        "armRegionName": catalog.region, "period_hours": decimal_text(scenario.period_hours),
        "snapshot_kind": catalog.kind, "consulted_at": catalog.consulted_at,
        "price_source": catalog.source_url,
        "base": {"label": scenario.base_label, "compute_usd": decimal_text(base), "resources": base_rows},
        "optimized": {"label": scenario.optimized_label, "compute_usd": decimal_text(proposed), "resources": proposed_rows},
        "potential_savings_usd": decimal_text(savings),
        "potential_savings_percent": None if percent is None else decimal_text(percent),
        "percentage_note": "undefined_baseline_zero" if base == 0 else "base_minus_proposed_divided_by_base",
        "scope": "VM compute only; no disks, network, extra licenses or taxes",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare custos potenciais de VMs Linux em Brazil South, offline.")
    parser.add_argument("scenario", nargs="?", type=Path, default=ROOT / "examples/laboratorio.json")
    parser.add_argument("--catalog", type=Path, default=ROOT / "catalog/prices-brazilsouth.json")
    parser.add_argument("--json", action="store_true", help="Saída estruturada, com valores Decimal como texto")
    args = parser.parse_args(argv)
    try:
        catalog = validate_catalog(read_json(args.catalog))
        scenario = validate_scenario(read_json(args.scenario), catalog)
        report = compare(catalog, scenario)
        report["catalog_sha256"] = hashlib.sha256(args.catalog.read_bytes()).hexdigest()
    except (ValidationError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(report["title"])
        print(f"Brazil South | USD | {report['period_hours']} horas no período")
        print(f"Catálogo: {report['snapshot_kind']} | consulta UTC {report['consulted_at']}")
        print(f"Base: USD {report['base']['compute_usd']}")
        print(f"Proposto: USD {report['optimized']['compute_usd']}")
        print(f"Diferença potencial (base - proposto): USD {report['potential_savings_usd']}")
        percent = report["potential_savings_percent"]
        print("Percentual: não definido, base zero" if percent is None else f"Percentual potencial: {percent}%")
        if Decimal(report["potential_savings_usd"]) < 0:
            print("O cenário proposto aumenta o custo de compute sob estas premissas.")
        print("Escopo: compute de VM; discos, rede, licenças adicionais e impostos não incluídos.")
        print("Estimativa para decisão; não representa fatura ou economia realizada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
