import io
import re
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch
from scripts import score_claude_comparison as job


class ScoringJobTests(unittest.TestCase):
    def test_sql_wildcards_are_parameters_and_zero_mature_is_not_zero_captured(self):
        backend=job.backend
        conn=MagicMock()
        cur=conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchall.side_effect=[[],[]]
        cur.fetchone.return_value=(1,0)
        with patch.object(backend,"ensure_backend_schema"),patch.object(backend,"db_connection",return_value=conn),redirect_stdout(io.StringIO()) as out:
            job.main()
        for call in cur.execute.call_args_list:
            sql,args=call.args
            self.assertEqual(re.findall(r"%(?!s)",sql),[])
            self.assertEqual(sql.count("%s"),len(args))
        self.assertEqual(cur.execute.call_args_list[0].args[1][0],"claude_pair:%")
        self.assertIn('"captured_pair_count": 1',out.getvalue())
        self.assertIn('"eligible_pair_count": 0',out.getvalue())
        self.assertIn('"calendar_sale": false',out.getvalue())


if __name__=="__main__":unittest.main()
