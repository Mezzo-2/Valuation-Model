"""汇率方向、北京时间运行日期、前一天兜底及每次重新查询。"""
from copy import deepcopy
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from valuation.historical_financials.fx import SOURCE, INDICATORS, ensure_valuation_fx, fetch_valuation_fx, parse_fixing_rates, run_date
from valuation.shared.io import dump_json, load_json
from valuation.shared.markets import valuation_factor, valuation_fx_note
from valuation.tests.test_foreign_financials import integration_spec
from valuation.research_dossier.snapshot import snapshot_from_spec

AS_OF=date(2025,10,27)


def reply(currency="HKD", rows=None):
    name=INDICATORS[currency]
    rows=[("2025-10-26",.85),("2025-10-25",.86)] if rows is None else rows
    return {"code":1,"data":{"answer":f"|日期|{name}|\n|---|---|\n"+"\n".join(f"|{d}|{v}|" for d,v in rows),
            "indicator_description":[{"指标名称":name,"数据来源":SOURCE,"频率":"D"}]}}


class ValuationFxTests(unittest.TestCase):
    def test_accepts_same_day_and_excludes_future_stale_invalid_quotes(self):
        raw=reply(rows=[("2025-10-28",.5),("2025-10-27",.7),("2025-10-26",.85),("2025-10-25",.86),
                        ("2025-10-23",0),("2025-10-22",-1),("2025-10-21","nan"),("2025-10-01",.9)])
        self.assertEqual(parse_fixing_rates(raw,INDICATORS["HKD"],AS_OF),{date(2025,10,27):.7,date(2025,10,26):.85})

    def test_rejects_wrong_indicator_source_and_hundred_unit(self):
        for field,value in [("指标名称","人民币兑港元"),("数据来源","其他"),("频率","M"),("单位","人民币/100港元")]:
            raw=reply();raw["data"]["indicator_description"][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                parse_fixing_rates(raw,INDICATORS["HKD"],AS_OF)

    def test_inverse_direction_and_direct_direction(self):
        mcp=Mock();mcp.call.return_value=reply()
        with tempfile.TemporaryDirectory() as directory:
            fx=fetch_valuation_fx(mcp,"CNY","HKD",AS_OF,Path(directory))
            self.assertAlmostEqual(fx["rate"],1/.85)
            self.assertEqual(fx["date"],"2025-10-26")
            inverse=fetch_valuation_fx(mcp,"HKD","CNY",AS_OF,Path(directory))
            self.assertEqual(inverse["rate"],.85)

    def test_cross_rates_use_latest_common_date(self):
        mcp=Mock();mcp.call.side_effect=[reply("HKD",[("2025-10-27",.85),("2025-10-26",.86)]),reply("USD",[("2025-10-26",7)])]
        with tempfile.TemporaryDirectory() as directory:
            fx=fetch_valuation_fx(mcp,"USD","HKD",AS_OF,Path(directory))
        self.assertEqual(fx["date"],"2025-10-26")
        self.assertAlmostEqual(fx["rate"],7/.86)

    def test_each_call_refreshes_without_changing_financials(self):
        spec=integration_spec("HK");facts=spec["facts"]
        facts["market"]["报表币种兑交易币种"]=999
        facts["market"]["快照日期"]="2020-01-01"
        facts["valuation_fx"]={"rate":999,"date":"2020-01-01"}
        before=deepcopy(facts["income"])
        with tempfile.TemporaryDirectory() as directory,patch("valuation.historical_financials.fx.run_date",return_value=AS_OF),patch("valuation.historical_financials.fx.ComeinClient") as client:
            run=Path(directory);dump_json(run/"facts.json",facts)
            client.return_value.__enter__.return_value.call.return_value=reply()
            ensure_valuation_fx(run)
            saved=load_json(run/"facts.json")
            self.assertEqual(saved["income"],before)
            self.assertAlmostEqual(valuation_factor(saved),1/.85)
            self.assertEqual(saved["valuation_fx"]["source"],SOURCE)
            self.assertEqual(saved["valuation_fx"]["as_of"],AS_OF.isoformat())
            spec["facts"]=saved
            snapshot=snapshot_from_spec(spec)
            self.assertIn("1.176471 HKD",valuation_fx_note(snapshot))
            self.assertNotIn("None",valuation_fx_note(snapshot))
            client.return_value.__enter__.return_value.call.return_value=reply(rows=[("2025-10-27",.86)])
            ensure_valuation_fx(run)
            self.assertEqual(client.call_count,2)
            self.assertEqual(client.return_value.__enter__.return_value.call.call_count,2)
            refreshed=load_json(run/"facts.json")
            self.assertAlmostEqual(refreshed["market"]["报表币种兑交易币种"],1/.86)
            self.assertEqual(refreshed["income"],before)

    def test_a_share_and_same_currency_do_not_fetch_or_rewrite(self):
        for facts in [{"ticker":"300308"},{"market_type":"US","currency":"USD","trading_currency":"USD"}]:
            with self.subTest(facts=facts),tempfile.TemporaryDirectory() as directory,patch("valuation.historical_financials.fx.ComeinClient") as client:
                path=Path(directory)/"facts.json";dump_json(path,facts);before=path.read_bytes()
                ensure_valuation_fx(Path(directory))
                self.assertEqual(path.read_bytes(),before)
                client.assert_not_called()

    def test_unavailable_quote_never_defaults_to_one(self):
        facts={"market_type":"HK","currency":"CNY","trading_currency":"HKD","as_of":"2025-10-27"}
        with tempfile.TemporaryDirectory() as directory,patch("valuation.historical_financials.fx.ComeinClient") as client:
            run=Path(directory);dump_json(run/"facts.json",facts)
            client.return_value.__enter__.return_value.call.return_value=reply(rows=[("2025-09-01",.8)])
            self.assertIsNone(ensure_valuation_fx(run,required=False))
            with self.assertRaises(SystemExit):ensure_valuation_fx(run)
            self.assertNotIn("报表币种兑交易币种",load_json(run/"facts.json").get("market",{}))

    def test_run_date_uses_shanghai_timezone(self):
        with patch("valuation.historical_financials.fx.datetime") as clock:
            clock.now.return_value.date.return_value=AS_OF
            self.assertEqual(run_date(),AS_OF)
            self.assertEqual(clock.now.call_args.args[0].key,"Asia/Shanghai")

    def test_today_is_preferred(self):
        mcp=Mock();mcp.call.return_value=reply(rows=[("2025-10-26",.86),("2025-10-27",.85)])
        with tempfile.TemporaryDirectory() as directory:
            fx=fetch_valuation_fx(mcp,"HKD","CNY",AS_OF,Path(directory))
        self.assertEqual(fx["date"],AS_OF.isoformat())
        self.assertEqual(fx["rate"],.85)

    def test_cross_rates_cannot_mix_dates(self):
        mcp=Mock();mcp.call.side_effect=[reply("HKD",[("2025-10-27",.85)]),reply("USD",[("2025-10-26",7)])]
        with tempfile.TemporaryDirectory() as directory,self.assertRaisesRegex(ValueError,"同日"):
            fetch_valuation_fx(mcp,"USD","HKD",AS_OF,Path(directory))

    def test_month_boundary_queries_previous_month(self):
        mcp=Mock();mcp.call.return_value=reply(rows=[("2025-10-31",.85),("2025-10-30",.86)])
        with tempfile.TemporaryDirectory() as directory:
            fx=fetch_valuation_fx(mcp,"HKD","CNY",date(2025,11,1),Path(directory))
        self.assertEqual(fx["date"],"2025-10-31")
        self.assertEqual(mcp.call.call_args.args[1]["start"],"202510")
        self.assertEqual(mcp.call.call_args.args[1]["end"],"202511")

    def test_weekend_does_not_extend_past_previous_calendar_day(self):
        with self.assertRaisesRegex(ValueError,"当天及前一天"):
            parse_fixing_rates(reply(rows=[("2025-10-31",.85)]),INDICATORS["HKD"],date(2025,11,2))

    def test_conflicting_same_day_values_are_rejected(self):
        with self.assertRaisesRegex(ValueError,"同日汇率值冲突"):
            parse_fixing_rates(reply(rows=[("2025-10-27",.85),("2025-10-27",.86)]),INDICATORS["HKD"],AS_OF)

    def test_failed_refresh_removes_old_fx(self):
        facts={"market_type":"HK","currency":"CNY","trading_currency":"HKD",
               "market":{"报表币种兑交易币种":1.2,"每交易单位普通股数":1},
               "valuation_fx":{"base":"CNY","quote":"HKD","rate":1.2}}
        with tempfile.TemporaryDirectory() as directory,patch("valuation.historical_financials.fx.run_date",return_value=AS_OF),patch("valuation.historical_financials.fx.ComeinClient") as client:
            run=Path(directory);dump_json(run/"facts.json",facts)
            client.return_value.__enter__.return_value.call.return_value=reply(rows=[("2025-10-24",.8)])
            self.assertIsNone(ensure_valuation_fx(run,required=False))
            saved=load_json(run/"facts.json")
            self.assertNotIn("valuation_fx",saved)
            self.assertNotIn("报表币种兑交易币种",saved["market"])
            with self.assertRaises(ValueError):valuation_factor(saved)
            self.assertEqual(load_json(run/"logs"/"valuation_fx_error.json")["as_of"],AS_OF.isoformat())

    def test_same_currency_clears_obsolete_fx(self):
        facts={"market_type":"US","currency":"USD","trading_currency":"USD",
               "market":{"每交易单位普通股数":1,"报表币种兑交易币种":8},
               "valuation_fx":{"base":"USD","quote":"HKD","rate":8}}
        with tempfile.TemporaryDirectory() as directory,patch("valuation.historical_financials.fx.ComeinClient") as client:
            run=Path(directory);dump_json(run/"facts.json",facts)
            ensure_valuation_fx(run)
            saved=load_json(run/"facts.json")
            self.assertNotIn("valuation_fx",saved)
            self.assertNotIn("报表币种兑交易币种",saved["market"])
            self.assertEqual(valuation_factor(saved),1)
            self.assertEqual(valuation_fx_note(saved),"")
            client.assert_not_called()

    def test_successful_refresh_clears_old_error(self):
        facts={"market_type":"HK","currency":"CNY","trading_currency":"HKD"}
        with tempfile.TemporaryDirectory() as directory,patch("valuation.historical_financials.fx.run_date",return_value=AS_OF),patch("valuation.historical_financials.fx.ComeinClient") as client:
            run=Path(directory);dump_json(run/"facts.json",facts)
            error=run/"logs"/"valuation_fx_error.json";dump_json(error,{"reason":"old failure"})
            client.return_value.__enter__.return_value.call.return_value=reply()
            ensure_valuation_fx(run)
            self.assertFalse(error.exists())


if __name__=="__main__":unittest.main()
