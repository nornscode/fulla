from fulla.facts import FactStore


def test_add_and_recall(tmp_path):
    store = FactStore(tmp_path / "facts.json")
    f = store.add("Mum's birthday is 1961-07-15")
    assert f.id == 1
    assert [x.text for x in store.search("birthday mum")] == ["Mum's birthday is 1961-07-15"]
    assert store.search("dentist") == []


def test_persists_across_instances(tmp_path):
    path = tmp_path / "facts.json"
    FactStore(path).add("the starter lives in the fridge")
    again = FactStore(path)
    assert len(again.all()) == 1
    assert again.add("second").id == 2


def test_forget(tmp_path):
    store = FactStore(tmp_path / "facts.json")
    f = store.add("temporary")
    assert store.remove(f.id) is True
    assert store.remove(f.id) is False
    assert store.all() == []


def test_empty_query_lists_everything(tmp_path):
    store = FactStore(tmp_path / "facts.json")
    store.add("a")
    store.add("b")
    assert len(store.search("")) == 2
