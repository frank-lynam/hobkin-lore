Test every filter you write on a small sample input with the jq command before handing it over:
`echo '<sample>' | jq -c '<filter>'`. When a filter fails, the error names the value it couldn't
index: check it against `symbol` for the function involved.
