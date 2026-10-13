Third-party notices
===================

whatsapp-agent is MIT licensed (see LICENSE). It includes or uses the following
third-party software.

1. wwebjs-api — https://github.com/avoylenko/wwebjs-api
   wwebjs-api/sessions.js is a modified copy of src/sessions.js from wwebjs-api
   v1.34.7-hotfix2. The modifications are marked "cantonese.ai patch" in the file.
   docker-compose.yml and k8s/ run the unmodified upstream image
   avoylenko/wwebjs-api:v1.34.7-hotfix2 with that one file mounted over it.

   MIT License

   Copyright (c) 2024 Anton Voylenko

   Permission is hereby granted, free of charge, to any person obtaining a copy
   of this software and associated documentation files (the "Software"), to deal
   in the Software without restriction, including without limitation the rights
   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
   copies of the Software, and to permit persons to whom the Software is
   furnished to do so, subject to the following conditions:

   The above copyright notice and this permission notice shall be included in all
   copies or substantial portions of the Software.

   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
   SOFTWARE.

2. whatsapp-web.js — https://github.com/wwebjs/whatsapp-web.js (Apache License 2.0)
   Used by the wwebjs-api image; not redistributed in this repository.

This project is not affiliated with, endorsed by or sponsored by WhatsApp or Meta.
"WhatsApp" is a trademark of WhatsApp LLC.
