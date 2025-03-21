import builtins
import json
import logging
import operator
import os
import random
import shutil
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional, Set, Any, Dict, List, Callable

from flatlatex import converter
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from pygments.formatters import HtmlFormatter
from quart import send_from_directory, Response, redirect
from quart_trio import QuartTrio
from rich.logging import RichHandler
import minify_html

from . import config as default_config
from .config import ingest_dir
from .crosslink import IngestedBlobs, RefInfo, find_all_refs
from .graphstore import GraphStore, Key
from .take2 import RefInfo, encoder, Section
from .utils import progress, dummy_progress
from .tree import TreeVisitor
from . import take2

FORMAT = "%(message)s"
logging.basicConfig(
    level="INFO", format=FORMAT, datefmt="[%X]", handlers=[RichHandler()]
)

log = logging.getLogger("papyri")

CSS_DATA = HtmlFormatter(style="pastie").get_style_defs(".highlight")


def minify(s):
    return minify_html.minify(
        s, minify_js=True, remove_processing_instructions=True, keep_closing_tags=True
    )


def url(info, prefix, suffix):
    assert isinstance(info, RefInfo), info
    assert info.kind in (
        "module",
        "api",
        "examples",
        "assets",
        "?",
        "docs",
        "to-resolve",
    ), repr(info)
    # assume same package/version for now.
    assert info.module is not None
    if info.module is None:
        assert info.version is None
        return info.path + suffix
    if info.kind == "module":
        return f"{prefix}{info.module}/{info.version}/api/{info.path}"
    if info.kind == "examples":
        return f"{prefix}{info.module}/{info.version}/examples/{info.path}"
    else:
        return f"{prefix}{info.module}/{info.version}/{info.kind}/{info.path}{suffix}"


def unreachable(*obj):
    return str(obj[1])
    assert False, f"Unreachable: {obj=}"


class CleanLoader(FileSystemLoader):
    """
    A loader for ascii/ansi that remove all leading spaces and pipes  until the last pipe.
    """

    def get_source(self, *args, **kwargs):
        (source, filename, uptodate) = super().get_source(*args, **kwargs)
        return until_ruler(source), filename, uptodate


def until_ruler(doc):
    """
    Utilities to clean jinja template;

    Remove all ``|`` and `` `` until the last leading ``|``

    """
    lines = doc.split("\n")
    new = []
    for l in lines:

        while len(l.lstrip()) >= 1 and l.lstrip()[0] == "|":
            l = l.lstrip()[1:]
        new.append(l)
    return "\n".join(new)


# here we compute the siblings at each level; as well as one level down
# this is far from efficient and a hack, but it helps with navigation.
# I'm pretty sure we load the full library while we could
# load only the current module id not less, and that this could
# be done at ingest time or cached.
# So basically in the breadcrumps
# IPython.lib.display.+
#  - IPython will be siblings with numpy; scipy, dask, ....
#  - lib (or "IPython.lib"), with "core", "display", "terminal"...
#  etc.
#  - + are deeper children's
#
# This is also likely a bit wrong; as I'm sure we want to only show
# submodules or sibling modules and not attribute/instance of current class,
# though that would need loading the files and looking at the types of
# things. likely want to store that in a tree somewhere But I thing this is
# doable after purely as frontend thing.


def compute_siblings_II(ref, family: set):
    """ """
    from collections import defaultdict

    module_versions = defaultdict(lambda: set())
    for f in family:
        module_versions[f.module].add(f.version)

    module_versions_max = {k: max(v) for k, v in module_versions.items()}

    family = {f for f in family if f.version == module_versions_max[f.module]}

    parts = ref.split(".") + ["+"]
    siblings = OrderedDict()
    cpath = ""

    # TODO: move this at ingestion time for all the non-top-level.
    for i, part in enumerate(parts):
        candidates = [c for c in family if c.path.startswith(cpath) and "." in c.path]
        # trm down to the right length
        candidates = [
            RefInfo(c.module, c.version, "api", ".".join(c.path.split(".")[: i + 1]))
            for c in candidates
        ]
        sib = list(sorted(set(candidates), key=operator.attrgetter("path")))

        siblings[part] = [(c, c.path.split(".")[-1]) for c in sib]
        cpath += part + "."
    if not siblings["+"]:
        del siblings["+"]
    return siblings


def make_tree(names):

    rd = lambda: defaultdict(rd)
    tree = defaultdict(rd)

    for n in names:
        parts = n.split(".")
        branch = tree
        for p in parts:
            branch = branch[p]
    return tree


def cs2(ref, tree, ref_map):
    """
    IIRC this is quite similar to compute_siblings(_II),
    but more efficient as we know we are going to compute
    all the siblings and not just the local one when rendering a single page.

    """
    parts = ref.split(".") + ["+"]
    siblings = OrderedDict()
    cpath = ""
    branch = tree

    def GET(ref_map, key, cpath):
        if key in ref_map:
            return ref_map[key]
        else:
            # this is a tiny bit weird; and will need a workaround at some
            # point.
            # this happends when one object in the hierarchy has not docs
            # (typically a class which is documented only in __init__)
            # or when a object does not match its qualified name (typically in
            # numpy, and __init__ with:
            #  from .foo import foo
            # leading to xxx.foo meaning being context dependant.
            # for now we return a dummy object.
            # print(f"{key=} seem to be a sibling with")
            # print(f"     {cpath=}, but was not ")
            # print(f"     found when trying to compute navigation for")
            # print(f"     {ref=}")
            # print(f"     Here are possible causes:")
            # print(f"        - it is a class and only __init__ has docstrings")
            # print(f"        - it is stored with the wrong qualname          ")
            # print(f"                                             ")

            return RefInfo("?", "?", "?", key)

    for p in parts:
        res = list(sorted((f"{cpath}{k}", k) for k in branch.keys() if k != "+"))
        if res:
            siblings[p] = [
                ##(ref_map.get(c, RINFO("?", "?", "?", c)), c.split(".")[-1])
                (GET(ref_map, c, cpath), c.split(".")[-1])
                for c, k in res
            ]
        else:
            break

        branch = branch[p]
        cpath += p + "."
    return siblings


def compute_graph(
    gs: GraphStore, backrefs: Set[Key], refs: Set[Key], key: Key
) -> Dict[Any, Any]:
    """
    Compute local reference graph for a given item.

    gs: Graphstore
        the graphstore to get data out of.
    backrefs:
        list of backreferences
    refs:
        list of forward references
    keys:
        current iten

    """
    weights = {}
    assert isinstance(backrefs, set)
    for b in backrefs:
        assert isinstance(b, Key)
    assert isinstance(refs, set)
    for f in refs:
        assert isinstance(f, Key)

    all_nodes = set(backrefs).union(set(refs))
    # all_nodes = set(Key(*k) for k in all_nodes)

    raw_edges = []
    for k in set(all_nodes):
        name = tuple(k)[3]
        neighbors_refs = gs.get_backref(k)
        weights[name] = len(neighbors_refs)
        orig = [x[3] for x in neighbors_refs]
        all_nodes = all_nodes.union(neighbors_refs)
        for o in orig:
            raw_edges.append((k.path, o))

    data: Dict[str, List[Any]] = {"nodes": [], "links": []}

    if len(weights) > 50:
        for thresh in sorted(set(weights.values())):
            log.debug("%s items ; remove items %s or lower", len(weights), thresh)
            weights = {k: v for k, v in weights.items() if v > thresh}
            log.debug("down to %s items", len(weights))
            if len(weights) < 50:
                break

    all_nodes = set(all_nodes)
    nums_ = set()
    edges = list(raw_edges)
    nodes = list(set(weights.keys()))
    for a, b in edges:
        if (a not in nodes) or (b not in nodes):
            continue
        nums_.add(a)
        nums_.add(b)
    nums = {x: i for i, x in enumerate(nodes, start=1)}

    for i, (from_, to) in enumerate(edges):
        if from_ == to:
            continue
        if from_ not in nodes:
            continue
        if to not in nodes:
            continue
        if key[3] in (to, from_):
            continue
        data["links"].append({"source": nums[from_], "target": nums[to], "id": i})

    for node in nodes:
        diam = 8.0
        if node == key[3]:
            continue
            diam = 18.0
        elif node in weights:
            import math

            diam = 8.0 + math.sqrt(weights[node])

        candidates = [n for n in all_nodes if n[3] == node and "??" not in n]
        if not candidates:
            uu = None
        else:
            # TODO : be smarter when we have multiple versions. Here we try to pick the latest one.
            latest_version = list(sorted(candidates))[-1]
            uu = url(RefInfo(*latest_version), "/p/", "")

        data["nodes"].append(
            {
                "id": nums[node],
                "val": diam,
                "label": node,
                "mod": ".".join(node.split(".")[0:1]),
                "url": uu,
            }
        )
    return data


class HtmlRenderer:
    def __init__(self, store: GraphStore, *, sidebar, prefix, trailing_html):
        assert prefix.startswith("/")
        assert prefix.endswith("/")
        self.progress = progress
        self.store = store
        self.env = Environment(
            loader=FileSystemLoader(os.path.dirname(__file__)),
            autoescape=select_autoescape(["html", "tpl.j2"]),
            undefined=StrictUndefined,
        )
        self.env.trim_blocks = True
        self.env.lstrip_blocks = True
        self.prefix = prefix
        suf = ".html" if trailing_html else ""
        self.env.globals["len"] = len
        self.env.globals["url"] = lambda x: url(x, prefix, suf)
        self.env.globals["unreachable"] = unreachable
        self.env.globals["sidebar"] = sidebar
        self.env.globals["dothtml"] = suf

    async def index(self):
        keys = self.store.glob((None, None, "meta", "aliases.cbor"))
        data = []
        for k in keys:
            meta = encoder.decode(self.store.get_meta(k))
            data.append((k.module, k.version, meta["logo"]))

        return self.env.get_template("index.tpl.j2").render(data=data)

    async def _write_index(self, html_dir):
        if html_dir:
            (html_dir / "index.html").write_text(await self.index())

    async def virtual(self, module, node):
        if module == "*":
            module = None
        items = list(self.store.glob((module, None, None, None)))

        visitor = TreeVisitor([getattr(take2, node)])
        acc = []
        for it in items:
            if it.kind in ("assets", "examples", "meta"):
                continue
            data = self.store.get(it)
            try:
                obj = encoder.decode(data)
            except Exception:
                print("Decode exception", it)
                continue
            if not isinstance(obj, IngestedBlobs):
                print("SKIP", it)
                continue
            lacc = []
            for a in obj.arbitrary + list(obj.content.values()):
                res = visitor.generic_visit(a)
                for v in res.values():
                    lacc.extend(v)
            if lacc:
                acc.append(Section(lacc, title=it[3]))
            # a2 = visitor.generic_visit(obj.content)
            # print(a1, a2)

        doc = IngestedBlobs.new()

        class S:
            pass

        doc.signature = S()
        doc.signature.value = None
        doc.arbitrary = acc
        return self.env.get_template("html.tpl.j2").render(
            graph=None,
            backrefs=[[], []],
            module="*",
            doc=doc,
            parts={"*": []},
            version="*",
            ext="",
            current_type="",
            logo=None,
            meta={},
        )

    async def _write_gallery(self, config):
        """ """
        mv2 = self.store.glob((None, None))
        for _, (module, version) in self.progress(
            set(mv2), description="Rendering galleries..."
        ):
            # version, module = item.path.name, item.path.parent.name
            data = await self.gallery(
                module,
                version,
                ext=".html",
            )
            if config.output_dir:
                (config.output_dir / module / version / "gallery").mkdir(
                    parents=True, exist_ok=True
                )
                (
                    config.output_dir / module / version / "gallery" / "index.html"
                ).write_text(data)

    async def gallery(self, package, version, ext=""):

        if package == version == "*":
            package = version = None

        figmap = defaultdict(lambda: [])
        assert isinstance(self.store, GraphStore)
        if package is not None:
            meta = encoder.decode(
                self.store.get_meta(Key(package, version, None, None))
            )
        else:
            meta = {"logo": None}
        logo = meta["logo"]
        res = self.store.glob((package, version, "assets", None))
        backrefs = set()
        for key in res:
            brs = {tuple(x) for x in self.store.get_backref(key)}
            backrefs = backrefs.union(brs)

        for key in backrefs:
            data = encoder.decode(self.store.get(Key(*key)))
            if "examples" in key:
                continue
            # TODO: examples can actuallly be just Sections.
            assert isinstance(data, IngestedBlobs)
            i = data

            for k in [
                u.value for u in i.example_section_data if u.__class__.__name__ == "Fig"
            ]:
                package, v, kind, _path = key
                # package, filename, link
                impath = f"{self.prefix}{k.module}/{k.version}/img/{k.path}"
                link = f"{self.prefix}/{package}/{v}/api/{_path}"
                # figmap.append((impath, link, name)
                figmap[package].append((impath, link, _path))

        glist = self.store.glob((package, version, "examples", None))
        for target_key in glist:
            section = encoder.decode(self.store.get(target_key))

            for k in [
                u.value for u in section.children if u.__class__.__name__ == "Fig"
            ]:
                package, v, _, _path = target_key

                # package, filename, link
                impath = f"{self.prefix}{k.module}/{k.version}/img/{k.path}"
                link = f"{self.prefix}{package}/{v}/examples/{_path}"
                name = _path
                figmap[package].append((impath, link, name))

        class D:
            pass

        doc = D()
        pap_keys = self.store.glob((None, None, "meta", "papyri.cbor"))
        parts = {package: []}
        for pk in pap_keys:
            mod, ver, kind, identifier = pk
            parts[package].append((RefInfo(mod, ver, "api", mod), mod))

        return self.env.get_template("gallery.tpl.j2").render(
            logo=logo,
            meta=meta,
            figmap=figmap,
            module=package,
            parts=parts,
            ext=ext,
            version=version,
            parts_links=defaultdict(lambda: ""),
            doc=doc,
        )

    async def _get_toc_for(self, package, version):
        keys = self.store.glob((package, version, "meta", "toc.cbor"))
        assert len(keys) == 1
        data = self.store.get(keys[0])
        return encoder.decode(data)

    async def _list_narative(self, package: str, version: str, ext=""):
        toctrees = await self._get_toc_for(package, version)

        meta = encoder.decode(self.store.get_meta(Key(package, version, None, None)))
        logo = meta["logo"]

        class D:
            pass

        doc = D()
        return self.env.get_template("toctree.tpl.j2").render(
            logo=logo,
            meta=meta,
            module=package,
            parts={},
            ext=ext,
            version=version,
            parts_links=defaultdict(lambda: ""),
            doc=doc,
            toctrees=toctrees,
        )

    async def _serve_narrative(self, package: str, version: str, ref: str):
        """
        Serve the narrative part of the documentation for given package
        """
        # return "Not Implemented"
        key = Key(package, version, "docs", ref)
        bytes = self.store.get(key)
        doc_blob = encoder.decode(bytes)
        meta = encoder.decode(self.store.get_meta(key))
        # return "OK"

        template = self.env.get_template("html.tpl.j2")

        assert isinstance(doc_blob, IngestedBlobs), type(doc_blob)

        toctrees = await self._get_toc_for(package, version)

        def open_toctree(toc, path):
            """
            Temporary, we really need a custom object.
            This mark specific toctree elements to be open in the rendering.
            Typically all nodes that link to current page.
            """
            for c in toc.children:
                if open_toctree(c, path):
                    toc.open = True
                # else:
                #    toc.open = False
            if toc.ref.path == path:
                toc.open = False
                toc.current = True
                return True
            return toc.open

        for t in toctrees:
            open_toctree(t, ref)

        return render_one(
            current_type="docs",
            meta=meta,
            template=template,
            doc=doc_blob,
            qa=package,  # incorrect
            ext="",
            parts={package: [], ref: []},
            parts_links={},
            backrefs=[],
            graph="{}",
            toctrees=toctrees,
        )

    async def _route_data(self, ref, version, known_refs):
        root = ref.split("/")[0].split(".")[0]
        key = Key(root, version, "module", ref)
        gbytes, backward, forward = self.store.get_all(key)
        x_, y_ = find_all_refs(self.store)
        doc_blob = encoder.decode(gbytes)
        return x_, y_, doc_blob, backward, forward

    async def _route(
        self,
        ref,
        version=None,
    ):
        assert not ref.endswith(".html")
        assert version is not None
        assert ref != ""

        template = self.env.get_template("html.tpl.j2")
        root = ref.split(".")[0]
        meta = encoder.decode(self.store.get_meta(Key(root, version, None, None)))

        known_refs, ref_map = find_all_refs(self.store)

        # technically incorrect we don't load backrefs
        x_, y_, doc_blob, backward, forward = await self._route_data(
            ref, version, known_refs
        )
        assert x_ == known_refs
        assert y_ == ref_map
        assert version is not None

        siblings = compute_siblings_II(ref, known_refs)  # type: ignore

        # End computing siblings.
        if True:  # handle if thing don't exists.
            # The reference we are trying to view exists;
            # we will now just render it.
            assert root is not None
            # assert version is not None
            data = compute_graph(
                self.store, backward, forward, Key(root, version, "module", ref)
            )
            json_str = json.dumps(data)
            parts_links = {}
            acc = ""
            for k in siblings.keys():
                acc += k
                parts_links[k] = acc
                acc += "."
            backrefs = [RefInfo(*k) for k in backward]
            return render_one(
                current_type="api",
                template=template,
                doc=doc_blob,
                qa=ref,
                ext="",
                parts=siblings,
                parts_links=parts_links,
                backrefs=backrefs,
                graph=json_str,
                meta=meta,
                toctrees=[],
            )
        else:
            # The reference we are trying to render does not exists
            # TODO
            error = self.env.get_template("404.tpl.j2")
            return error.render(backrefs=list(set()), tree={}, ref=ref, module=root)

    async def _write_api_file(
        self,
        tree,
        known_refs,
        ref_map,
        config,
        graph,
    ):

        template = self.env.get_template("html.tpl.j2")
        gfiles = list(self.store.glob((None, None, "module", None)))
        random.shuffle(gfiles)
        for _, key in progress(gfiles, description="Rendering API..."):
            module, version = key.module, key.version
            if config.ascii:
                await _ascii_render(key, store=self.store)
            if config.html:
                doc_blob, qa, siblings, parts_links, backward, forward = await loc(
                    key,
                    store=self.store,
                    tree=tree,
                    known_refs=known_refs,
                    ref_map=ref_map,
                )
                backward_r = [RefInfo(*x) for x in backward]
                if graph:
                    data = compute_graph(self.store, set(backward), set(forward), key)
                else:
                    data = {}
                json_str = json.dumps(data)
                meta = encoder.decode(self.store.get_meta(key))
                data = render_one(
                    current_type="API",
                    template=template,
                    doc=doc_blob,
                    qa=qa,
                    ext=".html",
                    parts=siblings,
                    parts_links=parts_links,
                    backrefs=backward_r,
                    graph=json_str,
                    meta=meta,
                    toctrees=[],
                )
                if config.output_dir:
                    (config.output_dir / module / version / "api").mkdir(
                        parents=True, exist_ok=True
                    )
                    tfile = config.output_dir / module / version / "api" / f"{qa}.html"
                    if config.minify:
                        tfile.write_text(minify(data))
                    else:
                        tfile.write_text(data)

    async def copy_static(self, output_dir):
        here = Path(os.path.dirname(__file__))
        _static = here / "static"
        for f in _static.glob("*"):
            bytes_ = f.read_bytes()
            assert output_dir is not None
            (output_dir.parent / f.name).write_bytes(bytes_)
        (output_dir.parent / "pygments.css").write_bytes(await pygment_css().get_data())

    async def copy_assets(self, config):
        """
        Copy assets from to their final destination.

        Assets are all the binary files that we don't want to change.
        """
        if config.output_dir is None:
            return

        assets_2 = self.store.glob((None, None, "assets", None))
        for _, asset in dummy_progress(assets_2, description="Copying assets"):
            b = config.output_dir / asset.module / asset.version / "img"
            b.mkdir(parents=True, exist_ok=True)
            data = self.store.get(asset)
            (b / asset.path).write_bytes(data)

    async def _write_example_files(self, config):
        if not config.html:
            return

        examples = list(self.store.glob((None, None, "examples", None)))
        for _, example in progress(examples, description="Rendering Examples..."):
            module, version, _, path = example
            data = await self.render_single_examples(
                module,
                version,
                ext=".html",
                data=self.store.get(example),
            )
            if config.output_dir:
                (config.output_dir / module / version / "examples").mkdir(
                    parents=True, exist_ok=True
                )
                (
                    config.output_dir / module / version / "examples" / f"{path}.html"
                ).write_text(data)

    async def _write_narrative_files(self, config):
        narrative = list(self.store.glob((None, None, "docs", None)))
        for (_, (module, version, _, path)) in progress(
            narrative, description="Rendering Narrative..."
        ):
            data = await self._serve_narrative(module, version, path)
            if config.output_dir:
                (config.output_dir / module / version / "docs").mkdir(
                    parents=True, exist_ok=True
                )
                (
                    config.output_dir / module / version / "docs" / f"{path}.html"
                ).write_text(data)

        tocs = set([(m, v) for m, v, _, _ in narrative])
        for module, version in tocs:
            print("toc for", module, version)
            toc = await self._list_narative(module, version, "")
            if config.output_dir:
                (config.output_dir / module / version / "docs" / "toc.html").write_text(
                    toc
                )

    async def examples_handler(self, package, version, subpath):

        meta = encoder.decode(self.store.get_meta(Key(package, version, None, None)))

        pap_keys = self.store.glob((None, None, "meta", "aliases.cbor"))
        parts = {package: []}
        for pk in pap_keys:
            mod, ver, _, _ = pk
            parts[package].append((RefInfo(mod, ver, "api", mod), mod))

        bytes_ = self.store.get(Key(package, version, "examples", subpath))

        ex = encoder.decode(bytes_)
        assert isinstance(ex, Section)

        class Doc:
            pass

        doc = Doc()

        return self.env.get_template("examples.tpl.j2").render(
            meta=meta,
            logo=meta["logo"],
            module=package,
            parts=parts,
            ext="",
            version=version,
            parts_links=defaultdict(lambda: ""),
            doc=doc,
            ex=ex,
        )

    async def render_single_examples(self, module, version, *, ext, data):

        mod_vers = self.store.glob((None, None))
        meta = encoder.decode(self.store.get_meta(Key(module, version, None, None)))
        logo = meta["logo"]
        parts = {module: []}
        for mod, ver in mod_vers:
            assert isinstance(mod, str)
            assert isinstance(ver, str)
            parts[module].append((RefInfo(mod, ver, "api", mod), mod))

        ex = encoder.decode(data)

        class Doc:
            pass

        doc = Doc()

        return self.env.get_template("examples.tpl.j2").render(
            meta=meta,
            logo=logo,
            module=module,
            parts=parts,
            ext=ext,
            version=version,
            parts_links=defaultdict(lambda: ""),
            doc=doc,
            ex=ex,
        )


async def img(package, version, subpath=None) -> Response:
    folder = ingest_dir / package / version / "assets"
    return await send_from_directory(folder, subpath)


def static(name) -> Callable[[], bytes]:
    here = Path(os.path.dirname(__file__))
    static = here / "static"

    async def f():
        return await send_from_directory(static, name)

    f.__name__ = name

    return f


def pygment_css() -> Response:
    return Response(CSS_DATA, mimetype="text/css")


def serve(*, sidebar: bool, port=1234):

    app = QuartTrio(__name__)

    gstore = GraphStore(ingest_dir)
    prefix = "/p/"
    html_renderer = HtmlRenderer(
        gstore, sidebar=sidebar, prefix=prefix, trailing_html=False
    )

    async def full(package, version, ref):
        if version == "*":
            res = list(html_renderer.store.glob([package, None]))
            assert len(res) == 1
            version = res[0][1]
            return redirect(f"{prefix}{package}/{version}/api/{ref}")
            # print(list(html_renderer.store.glob(
        return await html_renderer._route(ref, version)

    async def g(module):
        return await html_renderer.gallery(module)

    async def gr():
        return await html_renderer.gallery("*", "*")

    app.route("/logo.png")(static("papyri-logo.png"))
    app.route("/favicon.ico")(static("favicon.ico"))
    app.route("/papyri.css")(static("papyri.css"))
    app.route("/pygments.css")(pygment_css)
    app.route("/graph_canvas.js")(static("graph_canvas.js"))
    app.route("/graph_svg.js")(static("graph_svg.js"))
    # sub here is likely incorrect
    app.route(f"{prefix}<package>/<version>/img/<path:subpath>")(img)
    app.route(f"{prefix}<package>/<version>/examples/<path:subpath>")(
        html_renderer.examples_handler
    )
    app.route(f"{prefix}<package>/<version>/gallery")(html_renderer.gallery)
    app.route(f"{prefix}<package>/<version>/toc/")(html_renderer._list_narative)
    app.route(f"{prefix}<package>/<version>/docs/")(
        lambda package, version: redirect(f"{prefix}{package}/{version}/docs/index")
    )
    app.route(f"{prefix}<package>/<version>/docs/<ref>")(html_renderer._serve_narrative)
    app.route(f"{prefix}<package>/<version>/api/<ref>")(full)
    app.route(f"{prefix}<package>/static/<path:subpath>")(full)
    app.route(f"{prefix}/gallery/")(gr)
    app.route(f"{prefix}/gallery/<module>")(g)
    app.route(f"{prefix}/virtual/<module>/<node>")(html_renderer.virtual)
    app.route("/")(html_renderer.index)
    port = int(os.environ.get("PORT", port))
    print("Seen config port ", port)
    prod = os.environ.get("PROD", None)
    if prod:
        app.run(port=port, host="0.0.0.0")
    else:
        app.run(port=port)


def render_one(
    template,
    doc: IngestedBlobs,
    qa,
    ext,
    *,
    current_type,
    backrefs,
    parts=(),
    parts_links=(),
    graph="{}",
    meta,
    toctrees,
):
    """
    Return the rendering of one document

    Parameters
    ----------
    template
        a Jinja@ template object used to render.
    doc : DocBlob
        a Doc object with the informations for current obj
    qa : str
        fully qualified name for current object
    ext : str
        file extension for url  – should likely be removed and be set on the template
        I think that might be passed down to resolve maybe ?
    backrefs : list of str
        backreferences of document pointing to this.
    parts : Dict[str, list[(str, str)]
        used for navigation and for parts of the breakcrumbs to have navigation to siblings.
        This is not directly related to current object.
    parts_links : <Insert Type here>
        <Multiline Description Here>
    graph : <Insert Type here>
        <Multiline Description Here>
    logo : <Insert Type here>
        <Multiline Description Here>

    """
    assert isinstance(meta, dict)
    # TODO : move this to ingest likely.
    # Here if we have too many references we group them on where they come from.
    assert not hasattr(doc, "logo")
    if len(backrefs) > 30:

        b2 = defaultdict(lambda: [])
        for ref in backrefs:
            assert isinstance(ref, RefInfo)
            if "." in ref.path:
                mod, _ = ref.path.split(".", maxsplit=1)
            else:
                mod = ref.path
            b2[mod].append(ref)
        backrefs = (None, b2)
    else:
        backrefs = (backrefs, None)

    try:
        return template.render(
            current_type=current_type,
            doc=doc,
            logo=meta.get("logo", None),
            qa=qa,
            version=meta["version"],
            module=qa.split(".")[0],
            backrefs=backrefs,
            ext=ext,
            parts=parts,
            parts_links=parts_links,
            graph=graph,
            meta=meta,
            toctrees=toctrees,
        )
    except Exception as e:
        raise ValueError("qa=", qa) from e


@lru_cache
def _ascii_env():
    env = Environment(
        loader=CleanLoader(os.path.dirname(__file__)),
        lstrip_blocks=True,
        trim_blocks=True,
        undefined=StrictUndefined,
    )
    env.globals["len"] = len
    env.globals["unreachable"] = unreachable
    env.globals["sidebar"] = False
    try:

        c = converter()

        def math(s):
            assert isinstance(s, list)
            for x in s:
                assert isinstance(x, str)
            res = [c.convert(_) for _ in s]
            return res

        env.globals["math"] = math
    except ImportError:

        def math(s):
            return s + "($pip install flatlatex for unicode math)"

        env.globals["math"] = math

    template = env.get_template("ascii.tpl.j2")
    return env, template


async def _ascii_render(key: Key, store: GraphStore, known_refs=None, template=None):
    assert store is not None
    assert isinstance(store, GraphStore)
    ref = key.path

    env, template = _ascii_env()

    doc_blob = encoder.decode(store.get(key))
    meta = encoder.decode(store.get_meta(key))

    # exercise the reprs
    assert str(doc_blob)

    return render_one(
        current_type="API",
        meta=meta,
        template=template,
        doc=doc_blob,
        qa=ref,
        ext="",
        backrefs=[],
        graph="{}",
        toctrees=[],
    )


async def ascii_render(name, store=None):
    gstore = GraphStore(ingest_dir, {})
    key = next(iter(gstore.glob((None, None, "module", "papyri.examples"))))

    builtins.print(await _ascii_render(key, gstore))


async def loc(document: Key, *, store: GraphStore, tree, known_refs, ref_map):
    """
    return data for rendering in the templates

    Parameters
    ----------
    document: Store
        Path the document we need to read and prepare for rendering
    store: Store

        Store into which the document is stored (abstraciton layer over local
        filesystem or a remote store like github, thoug right now only local
        file system works)
    tree:
        tree of object we know about; this will be useful to compute siblings
        for the navigation menu at the top that allow to either drill down the
        hierarchy.
    known_refs: List[RefInfo]
        list of all the reference info for targets, so that we can resolve links
        later on; this is here for now, but shoudl be moved to ingestion at some
        point.
    ref_map: ??
        helper to compute the siblings for agiven hierarchy,

    Returns
    -------
    doc_blob: IngestedBlobs
        document that will be rendered
    qa: str
        fully qualified name of the object we will render
    siblings:
        information to render the navigation dropdown at the top.
    parts_links:
        information to render breadcrumbs with links to parents.


    Notes
    -----

    Note that most of the current logic assume we only have the documentation
    for a single version of a package; when we have multiple version some of
    these heuristics break down.

    """
    assert isinstance(document, Key), type(document)
    qa = document.path
    bytes_, backward, forward = store.get_all(document)
    doc_blob: IngestedBlobs = encoder.decode(bytes_)

    siblings = cs2(qa, tree, ref_map)

    parts_links = {}
    acc = ""
    for k in siblings.keys():
        acc += k
        parts_links[k] = acc
        acc += "."
    try:
        return doc_blob, qa, siblings, parts_links, backward, forward
    except Exception as e:
        raise type(e)(f"Error in {qa}") from e


@dataclass
class StaticRenderingConfig:
    """Class for keeping track of an item in inventory."""

    html: bool
    html_sidebar: bool
    ascii: bool
    output_dir: Optional[Path]
    minify: bool


async def main(ascii: bool, html, dry_run, sidebar: bool, graph: bool, minify: bool):
    """
    This does static rendering of all the given files.

    Parameters
    ----------
    ascii: bool
        whether to render ascii files.
    html: bool
        whether to render the html
    dry_run: bool
        do not write the output.
    Sidebar:bool
        render the sidebar in html
    graph: bool

    """

    html_dir_: Optional[Path] = default_config.html_dir
    if dry_run:
        output_dir = None
        html_dir_ = None
    else:
        assert html_dir_ is not None
        output_dir = html_dir_ / "p"
        output_dir.mkdir(exist_ok=True)
    config = StaticRenderingConfig(html, sidebar, ascii, output_dir, minify)
    prefix = "/p/"

    gstore = GraphStore(ingest_dir, {})

    known_refs, ref_map = find_all_refs(gstore)
    # end

    family = frozenset(_.path for _ in known_refs)

    tree = make_tree(family)
    if html_dir_ is not None:
        log.info("going to erase %s", html_dir_)
        shutil.rmtree(html_dir_)
    else:
        log.info("no output dir, we'll try not to touch the filesystem")

    # shuffle files to detect bugs, just in case.
    # Gallery

    html_renderer = HtmlRenderer(
        gstore, sidebar=config.html_sidebar, prefix=prefix, trailing_html=True
    )
    await html_renderer._write_gallery(config)

    await html_renderer._write_example_files(config)
    await html_renderer._write_index(html_dir_)
    await html_renderer.copy_assets(config)
    await html_renderer.copy_static(config.output_dir)
    await html_renderer._write_narrative_files(config)

    await html_renderer._write_api_file(
        tree,
        known_refs,
        ref_map,
        config,
        graph,
    )
