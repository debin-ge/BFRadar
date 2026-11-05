import xml.etree.ElementTree as ET

import pytest

from app.textutil import Link, content_hash, make_excerpt, normalize_url, structure_html
from conftest import snapshot

CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"


def text(html):
    return structure_html(html).text


def test_table_rows_become_pipe_lines():
    html = "<p>Plans</p><table><tr><th>Plan</th><th>RAM</th></tr><tr><td>KVM-1</td><td>1 GB</td></tr></table><p>After</p>"
    assert text(html) == "Plans\nPlan | RAM\nKVM-1 | 1 GB\nAfter"


def test_nested_lists_are_indented():
    html = "<ul><li>A<ul><li>A1</li><li>A2</li></ul></li><li>B</li></ul>"
    assert text(html) == "- A\n  - A1\n  - A2\n- B"


def test_links_are_numbered_and_recorded():
    html = ('<p>Order <a href="https://x.example/cart?pid=1">Order Now</a> or '
            '<a href="#top">top</a> <a href="">empty</a> <a href="https://x.example/2">Second\n link</a></p>')
    st = structure_html(html)
    assert st.text == "Order Order Now [1] or top empty Second link [2]"
    assert st.links == [Link(1, "Order Now", "https://x.example/cart?pid=1"), Link(2, "Second link", "https://x.example/2")]


def test_pre_keeps_line_breaks_and_br_splits_lines():
    html = "<p>line1<br>line2<br/>line3</p><pre>a  ||  b\n c || d</pre>"
    assert text(html) == "line1\nline2\nline3\na || b\n c || d"  # 行内连续空格统一压缩


def test_entities_scripts_images_and_blank_lines():
    html = ('<p>A&amp;B&nbsp;C &euro;21</p><script>var x = 1;</script><style>p{}</style>'
            '<p><img src="s.png" alt=":star:"> Deals <img src="d.png" alt="31% OFF with code X"></p>'
            '<p></p><p></p><p>End</p>')
    assert text(html) == "A&B C €21\nDeals [img: 31% OFF with code X]\nEnd"


def test_real_pipe_post_from_fixture():
    root = ET.parse(snapshot("lowendtalk_offers_2026-11-02.xml")).getroot()
    item = next(i for i in root.iter("item") if i.findtext("guid") == "222164@/discussions")
    st = structure_html(item.findtext(CONTENT) or item.findtext("description"))  # LET 全文在 description
    assert "AMD EPYC || 2vCore || 4GB RAM || 40GB NVMe || 4TB BW/mo|| 1Gbit Port || 1 IPV4/1 IPV6 ||" in st.text
    assert "Price: €21/ Annually || Order Now [2] ||" in st.text
    assert st.links[1] == Link(2, "Order Now",
                               "https://hostdzire.com/billing/cart.php?a=add&pid=1261&currency=1&billingcycle=annually")


@pytest.mark.parametrize("url, expected", [
    ("https://lowendtalk.com/discussion/222765/black-friday-uk-vps?utm_source=rss#latest",
     "https://lowendtalk.com/discussion/222765"),
    ("http://www.lowendtalk.com/discussion/222765/slug/p2", "https://lowendtalk.com/discussion/222765"),
    ("https://www.lowendspirit.com/discussion/11544/black-friday", "https://lowendspirit.com/discussion/11544"),
    ("https://nodeseek.com/post-1027323-3", "https://www.nodeseek.com/post-1027323-1"),
    ("https://old.reddit.com/r/VPS/comments/1abcde/some_title/", "https://www.reddit.com/r/VPS/comments/1abcde"),
    ("https://lowendtalk.com/discussion/comment/3139017/#Comment_3139017",
     "https://lowendtalk.com/discussion/comment/3139017/"),
    ("https://lowendbox.com/blog/cheap-vps-deals/", "https://lowendbox.com/blog/cheap-vps-deals/"),
    ("https://Example.COM:443/a?b=1&utm_medium=x&fbclid=y&b=2", "https://example.com/a?b=1&b=2"),
    ("https://clients.hostsailor.com/r.php?ref=halloween26", "https://clients.hostsailor.com/r.php?ref=halloween26"),
])
def test_normalize_url(url, expected):
    assert normalize_url(url) == expected


def test_content_hash_ignores_whitespace_only():
    assert content_hash("Title", "a  b\n c") == content_hash("Title ", "a b c")
    assert content_hash("Title", "a b c") != content_hash("Title", "a b d")
    assert content_hash("ab", "c") != content_hash("a", "bc")


def test_make_excerpt():
    assert make_excerpt("line 1\n\nline 2") == "line 1 / line 2"
    long = "Black Friday deals. " * 40
    out = make_excerpt(long, limit=100)
    assert len(out) <= 100
    assert out.endswith("…")
    assert out.startswith("Black Friday deals.")
