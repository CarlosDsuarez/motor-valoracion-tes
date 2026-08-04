# Motor de Valoración — atajos del ciclo de trabajo.
#
# El intérprete del venv se usa siempre de forma explícita en vez de activar el
# entorno, para que cada target sea reproducible sin depender del shell.

PYTHON      := ./.venv/bin/python
PYTHON_BASE := python3.13

.DEFAULT_GOAL := help
.PHONY: help setup test cov fetch calibrate validate excel xlsm todo limpiar

help:  ## Muestra esta ayuda
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup:  ## Crea el venv e instala el proyecto con todos los extras
	$(PYTHON_BASE) -m venv .venv
	$(PYTHON) -m pip install --quiet --upgrade pip
	$(PYTHON) -m pip install -e ".[benchmark,socrata,excel,dev]"
	@echo "Listo. Verificá con: make test"

test:  ## Corre la suite completa
	$(PYTHON) -m pytest tests/ -q

cov:  ## Corre la suite con reporte de cobertura
	$(PYTHON) -m pytest tests/ --cov=motor_tes --cov-report=term-missing

fetch:  ## Descarga las fuentes y registra procedencia en data/manifest.json
	$(PYTHON) -m motor_tes.cli fetch

calibrate:  ## Calibra la curva y muestra los residuales por nodo
	$(PYTHON) -m motor_tes.cli calibrate

validate:  ## Genera validation/reporte_validacion.md con gráficos y benchmark
	$(PYTHON) -m motor_tes.cli validate

excel:  ## Exporta el libro .xlsx con los parámetros para las UDFs de VBA
	$(PYTHON) -m motor_tes.cli excel

xlsm:  ## Incorpora los módulos VBA y guarda el .xlsm (requiere Excel y permisos)
	$(PYTHON) excel/build_excel.py

todo: test validate excel  ## Suite, reporte de validación y libro Excel

limpiar:  ## Borra cachés y artefactos regenerables
	rm -rf .pytest_cache .coverage htmlcov build dist src/*.egg-info
	find . -path ./.venv -prune -o -name __pycache__ -type d -exec rm -rf {} +
