import http.client
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from p1006.ipp import Bridge, IPPError, Message, attr, create_server, encode_message, parse_message


def request(code, *attributes, data=b""):
    return Message((2,0), code, 123, [(1, [
        attr("attributes-charset", 0x47, "utf-8"),
        attr("attributes-natural-language", 0x48, "en"), *attributes])], data)


class TestWire(unittest.TestCase):
    def test_rfc8010_appendix_a1_request(self):
        # Transcribed field-by-field from RFC 8010 Appendix A.1, not generated
        # by encode_message. https://www.rfc-editor.org/rfc/rfc8010#appendix-A.1
        fixture = (
            b"\x01\x01\x00\x02\x00\x00\x00\x01\x01"
            b"\x47\x00\x12attributes-charset\x00\x05utf-8"
            b"\x48\x00\x1battributes-natural-language\x00\x05en-us"
            b"\x45\x00\x0bprinter-uri\x00\x2cipp://printer.example.com/ipp/print/pinetree"
            b"\x42\x00\x08job-name\x00\x06foobar"
            b"\x22\x00\x16ipp-attribute-fidelity\x00\x01\x01\x02"
            b"\x21\x00\x06copies\x00\x04\x00\x00\x00\x14"
            b"\x44\x00\x05sides\x00\x13two-sided-long-edge\x03%!PDF..."
        )
        parsed = parse_message(fixture)
        self.assertEqual(parsed.version, (1,1))
        self.assertEqual(parsed.code, 2)
        self.assertEqual(parsed.request_id, 1)
        self.assertEqual(parsed.get("copies"), 20)
        self.assertTrue(parsed.get("ipp-attribute-fidelity"))
        self.assertEqual(parsed.get("sides"), "two-sided-long-edge")
        self.assertEqual(parsed.data, b"%!PDF...")
        self.assertEqual(encode_message(parsed), fixture)

    def test_nested_collection_wire_and_continuation(self):
        # Independent manually framed media-col/media-size collection.
        fixture = (b"\x02\0\0\x04\0\0\0\1\x02"
                   b"\x34\0\x09media-col\0\0"
                   b"\x4a\0\0\0\x0amedia-size"
                   b"\x34\0\0\0\0"
                   b"\x4a\0\0\0\x0bx-dimension"
                   b"\x21\0\0\0\4\0\0\x52\x08"
                   b"\x4a\0\0\0\x0by-dimension"
                   b"\x21\0\0\0\4\0\0\x74\x04"
                   b"\x37\0\0\0\0\x37\0\0\0\0\x03")
        parsed = parse_message(fixture)
        size = parsed.get("media-col")[0].values[0]
        self.assertEqual([(x.name,x.values) for x in size], [("x-dimension",[21000]),("y-dimension",[29700])])
        self.assertEqual(encode_message(parsed), fixture)

    def test_truncated_invalid_integer_and_orphan_value(self):
        for wire in (b"", b"\2\0\0\4\0\0\0\1", b"\2\0\0\4\0\0\0\1\1\x21\0\1x\0\1\0\3",
                     b"\2\0\0\4\0\0\0\1\1\x44\0\0\0\1x\3"):
            with self.subTest(wire=wire), self.assertRaises(IPPError):
                parse_message(wire)


class TestBridge(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        def process(data, mime, options):
            self.calls.append((data,mime,options))
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("Test callback timed out")
            return {"submitted": True, "bytes_sent": 123}
        self.bridge = Bridge(process, Path(self.temp.name), 8631)

    def tearDown(self):
        self.release.set()
        self.bridge.close()
        self.temp.cleanup()

    def test_printer_capabilities_include_real_resolution(self):
        reply = self.bridge.handle(request(11))
        reply = parse_message(encode_message(reply))
        self.assertEqual(reply.code, 0)
        self.assertEqual(reply.get("printer-resolution-supported"), (1200,600,3))
        self.assertEqual(reply.get("printer-resolution-default"), (600,600,3))
        self.assertEqual(set(reply.values("printer-resolution-supported")), {(1200,600,3),(600,600,3),(300,300,3)})
        self.assertEqual(set(reply.values("pwg-raster-document-resolution-supported")), {(1200,600,3),(600,600,3),(300,300,3)})
        self.assertEqual(reply.get("sides-supported"), "one-sided")
        self.assertFalse(reply.get("color-supported"))
        self.assertIn("application/pdf", reply.values("document-format-supported"))
        self.assertIn("image/pwg-raster", reply.values("document-format-supported"))

    def test_windows_discovery_identity_has_uri_tag_and_bridge_pdls(self):
        reply = self.bridge.handle(request(11, attr("requested-attributes", 0x44,
                                   "printer-uuid", "printer-device-id")))
        parsed = parse_message(encode_message(reply))
        identity = parsed.get("printer-uuid")
        self.assertTrue(identity.startswith("urn:uuid:"))
        self.assertNotEqual(uuid.UUID(identity.removeprefix("urn:uuid:")).int, 0)
        wire_attributes = {a.name: a for group, attrs in parsed.groups if group == 4 for a in attrs}
        self.assertEqual(wire_attributes["printer-uuid"].tag, 0x45)
        self.assertEqual(wire_attributes["printer-device-id"].tag, 0x41)
        self.assertIn("CMD:PWGRaster,PDF;", parsed.get("printer-device-id"))
        self.assertNotIn("HBS", parsed.get("printer-device-id"))

    def test_printer_uuid_survives_restart_and_port_change(self):
        original = self.bridge.printer_uuid
        self.bridge.close()
        self.bridge = Bridge(lambda *args: {"submitted": False}, Path(self.temp.name), 9631)
        self.assertEqual(self.bridge.printer_uuid, original)
        recorded = json.loads((Path(self.temp.name)/"printer-identity.json").read_text())
        self.assertEqual("urn:uuid:" + recorded["printer_uuid"], original)

    def test_print_job_processing_then_transport_completed(self):
        reply = self.bridge.handle(request(2, attr("copies",0x21,2), data=b"%PDF-fixture"))
        self.assertEqual(reply.code, 0)
        job_id = reply.get("job-id")
        self.assertTrue(self.entered.wait(2))
        status = self.bridge.handle(request(9, attr("job-id",0x21,job_id)))
        self.assertEqual(status.get("job-state"),5)
        self.assertEqual(self.bridge.handle(request(8, attr("job-id",0x21,job_id))).code,0x0404)
        self.release.set()
        self.bridge.queue.join()
        status = self.bridge.handle(request(9, attr("job-id",0x21,job_id)))
        self.assertEqual(status.get("job-state"),9)
        self.assertEqual(status.get("job-state-reasons"),"job-completed-with-warnings")
        self.assertIn("not confirmed",status.get("job-state-message"))
        self.assertEqual(self.calls[0][1],"application/pdf")
        self.assertEqual(self.calls[0][2]["copies"],2)
        self.assertEqual((Path(self.temp.name)/"jobs"/str(job_id)/"document.bin").read_bytes(), b"%PDF-fixture")

    def test_create_send_cancel_and_job_listing(self):
        first = self.bridge.handle(request(5)).get("job-id")
        second = self.bridge.handle(request(5)).get("job-id")
        self.assertEqual(self.bridge.handle(request(8, attr("job-id",0x21,first))).code,0)
        self.assertEqual(self.bridge.handle(request(6, attr("job-id",0x21,second), attr("last-document",0x22,False), data=b"RaS2test")).code,0x0509)
        response = self.bridge.handle(request(6, attr("job-id",0x21,second), attr("last-document",0x22,True), data=b"RaS2test"))
        self.assertEqual(response.code,0)
        self.assertTrue(self.entered.wait(2))
        jobs = self.bridge.handle(request(10, attr("which-jobs",0x44,"all")))
        self.assertEqual(sum(group == 2 for group,_ in jobs.groups),2)
        self.assertEqual(self.bridge.handle(request(9,attr("job-id",0x21,999))).code,0x0406)

    def test_rejects_unsupported_output_settings_without_creating_job(self):
        for item in (attr("sides",0x44,"two-sided-long-edge"),attr("copies",0x21,100),
                     attr("printer-resolution",0x32,(1200,1200,3)),attr("media",0x44,"na_letter_8.5x11in"),
                     attr("page-ranges",0x33,(1,2)),attr("compression",0x44,"gzip")):
            with self.subTest(item=item.name):
                reply = self.bridge.handle(request(2,item,data=b"%PDF-test"))
                self.assertNotEqual(reply.code,0)
        self.assertEqual(self.bridge.jobs,{})

    def test_captured_windows_validate_and_create_job(self):
        # Exact output-affecting fields captured from Microsoft IPP Class Driver
        # on 2026-11-15, bridge.log 10:00:55.584/594. No hardware is involved.
        operations = [attr("attributes-charset",0x47,"utf-8"),
                      attr("attributes-natural-language",0x48,"ru-ru"),
                      attr("printer-uri",0x45,self.bridge.uri)]
        settings = [attr("copies",0x21,1),
                    attr("media-col",0x34,[attr("media-size",0x34,[
                        attr("x-dimension",0x21,20999),attr("y-dimension",0x21,29699)]),
                        attr("media-type",0x44,"stationery")]),
                    attr("print-color-mode",0x44,"monochrome"),
                    attr("print-quality",0x23,4),
                    attr("multiple-document-handling",0x44,"separate-documents-collated-copies"),
                    attr("sides",0x44,"one-sided"),attr("orientation-requested",0x23,3),
                    attr("printer-resolution",0x32,(300,300,3))]
        for operation in (4,5):
            operation_attrs = operations + ([attr("document-format",0x49,"application/pdf")] if operation == 4 else [])
            incoming = Message((2,0),operation,operation,[(1,operation_attrs),(2,settings)])
            reply = self.bridge.handle(parse_message(encode_message(incoming)))
            self.assertEqual(reply.code,0,reply.get("status-message"))
            if operation == 5:
                job = self.bridge.jobs[reply.get("job-id")]
                self.assertEqual(job["options"]["printer-resolution"],(300,300,3))
                self.assertEqual(job["options"]["media"],"iso_a4_210x297mm")
                self.assertEqual(job["state"],4)
        self.assertEqual(self.calls,[])

    def test_input_resolution_and_document_handling_boundaries(self):
        for resolution in ((300,300,3),(600,600,3),(1200,600,3)):
            self.assertEqual(self.bridge.handle(request(4,attr("printer-resolution",0x32,resolution))).code,0)
        for resolution in ((299,300,3),(600,300,3),(1200,1200,3),(300,300,4)):
            self.assertEqual(self.bridge.handle(request(4,attr("printer-resolution",0x32,resolution))).code,0x040B)
        for handling in ("single-document","separate-documents-collated-copies"):
            self.assertEqual(self.bridge.handle(request(4,attr("multiple-document-handling",0x44,handling))).code,0)
        for handling in ("separate-documents-uncollated-copies","single-document-new-sheet"):
            self.assertEqual(self.bridge.handle(request(4,attr("multiple-document-handling",0x44,handling))).code,0x040B)

    def test_a4_rounding_is_limited_to_integer_dimensions(self):
        def size(x=21000,y=29700,xtag=0x21):
            return [attr("media-size",0x34,[attr("x-dimension",xtag,x),attr("y-dimension",0x21,y)])]
        for x in (20999,21000,21001):
            for y in (29699,29700,29701):
                self.assertEqual(self.bridge.handle(request(4,attr("media-col",0x34,size(x,y)))).code,0)
        invalid = [size(20998),size(21002),size(y=29698),size(y=29702),size(20999,xtag=0x23),
                   size()+[attr("media-left-margin",0x21,399)],
                   size()+[attr("media-left-margin",0x21,401)],
                   size()+[attr("media-type",0x44,"photographic")],
                   size()+[attr("media-source",0x44,"main")]]
        duplicate_dimensions = [attr("x-dimension",0x21,20999),attr("x-dimension",0x21,21000),attr("y-dimension",0x21,29699)]
        invalid.append([attr("media-size",0x34,duplicate_dimensions)])
        invalid.append(size()+size(20999,29699))
        for collection in invalid:
            with self.subTest(collection=collection):
                self.assertEqual(self.bridge.handle(request(4,attr("media-col",0x34,collection))).code,0x040B)

    def test_dry_run_callback_does_not_report_completed(self):
        self.bridge.process_document = lambda *args: {"prepared":True}
        with self.assertLogs("p1006.ipp", level="ERROR"):
            reply = self.bridge.handle(request(2,data=b"%PDF-test"))
            self.bridge.queue.join()
        job = self.bridge.jobs[reply.get("job-id")]
        self.assertEqual(job["state"],8)
        self.assertIn("did not confirm",job["message"])
        self.assertEqual(job["result"],{"prepared":True})

    def test_http_and_ipp_uris_only_for_same_loopback_endpoint(self):
        for scheme in ("http","ipp"):
            self.assertEqual(self.bridge.handle(request(11,attr("printer-uri",0x45,
                             f"{scheme}://127.0.0.1:8631/ipp/print"))).code,0)
        for bad in ("http://127.0.0.1:9999/ipp/print","http://example.com:8631/ipp/print",
                    "https://127.0.0.1:8631/ipp/print","http://127.0.0.1:8631/wrong",
                    "http://user@127.0.0.1:8631/ipp/print","http://127.0.0.1:8631/ipp/print?x=1"):
            self.assertEqual(self.bridge.handle(request(11,attr("printer-uri",0x45,bad))).code,0x0406)
        created = self.bridge.handle(request(5))
        job_uri = created.get("job-uri").replace("ipp://","http://")
        self.assertEqual(self.bridge.handle(request(9,attr("job-uri",0x45,job_uri))).get("job-id"),created.get("job-id"))

    def test_media_collection_order_independent_and_duplicates_rejected(self):
        size = [attr("y-dimension",0x21,29700),attr("x-dimension",0x21,21000)]
        media = [attr("media-size",0x34,size)]
        self.assertEqual(self.bridge.handle(request(4,attr("media-col",0x34,media))).code,0)
        for invalid in ([media[0],media[0]],
                        [attr("media-size",0x34,size+[size[0]])],
                        [attr("media-size",0x34,size[:1])]):
            self.assertEqual(self.bridge.handle(request(4,attr("media-col",0x34,invalid))).code,0x040B)

    def test_restart_aborts_unfinished_job_and_preserves_id(self):
        job_id = self.bridge.handle(request(5)).get("job-id")
        self.bridge.close()
        metadata = Path(self.temp.name)/"jobs"/str(job_id)/"job.json"
        value = json.loads(metadata.read_text())
        value["state"] = 5  # Simulate crash while submitting; never replay.
        metadata.write_text(json.dumps(value))
        self.bridge = Bridge(lambda *args: self.fail("Unexpected replay"), Path(self.temp.name),8631)
        self.assertEqual(self.bridge.jobs[job_id]["state"],8)
        self.assertIn("not replayed",self.bridge.jobs[job_id]["message"])
        self.assertGreater(self.bridge.handle(request(5)).get("job-id"), job_id)


class TestHTTP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server(lambda *args:{"submitted":True},Path(self.temp.name),port=0,max_request=8192)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def connection(self):
        self.assertEqual(self.server.server_address[0],"127.0.0.1")
        return http.client.HTTPConnection(*self.server.server_address,timeout=3)

    def test_duplicate_start_does_not_restore_or_abort_live_jobs(self):
        job_id = self.server.bridge.handle(request(5)).get("job-id")
        metadata = Path(self.temp.name)/"jobs"/str(job_id)/"job.json"
        original = metadata.read_bytes()
        duplicate = None
        try:
            with self.assertRaises(OSError):
                duplicate = create_server(lambda *args: self.fail("Unexpected print"),
                                          Path(self.temp.name), port=self.server.server_port)
        finally:
            if duplicate is not None:
                duplicate.server_close()
        self.assertEqual(metadata.read_bytes(), original)
        self.assertEqual(self.server.bridge.jobs[job_id]["state"], 4)
        connection = self.connection()
        try:
            connection.request("POST", "/ipp/print", encode_message(request(11)),
                               {"Content-Type": "application/ipp"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(parse_message(response.read()).code, 0)
        finally:
            connection.close()

    def test_content_length_and_chunked_requests(self):
        wire = encode_message(request(11))
        for chunked in (False,True):
            with self.subTest(chunked=chunked):
                connection = self.connection()
                connection.request("POST","/ipp/print", iter([wire[:10],wire[10:]]) if chunked else wire,
                                   {"Content-Type":"application/ipp"},encode_chunked=chunked)
                response = connection.getresponse()
                self.assertEqual(response.status,200)
                parsed = parse_message(response.read())
                self.assertEqual(parsed.code,0)
                self.assertEqual(parsed.request_id,123)
                connection.close()

    def test_oversize_body_and_non_ipp_request(self):
        connection = self.connection()
        connection.request("POST","/ipp/print",b"x"*8193,{"Content-Type":"application/ipp"})
        response = connection.getresponse()
        self.assertEqual(response.status,413)
        response.read()
        connection.close()
        connection = self.connection()
        connection.request("POST","/ipp/print",b"x",{"Content-Type":"text/plain"})
        response = connection.getresponse()
        self.assertEqual(response.status,415)
        response.read()
        connection.close()


if __name__ == "__main__":
    unittest.main()
