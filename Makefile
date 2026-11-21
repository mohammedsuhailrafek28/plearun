test:
	python3 -m unittest discover -s tests -v

findings:
	python3 plearun_cli.py reports/report_fictional_02.txt records > findings.json
