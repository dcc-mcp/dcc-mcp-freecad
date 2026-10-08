# Changelog

## [0.8.0](https://github.com/dcc-mcp/dcc-mcp-freecad/compare/v0.7.0...v0.8.0) (2026-10-08)


### Features

* add bounded list_documents discovery under allowed roots ([3181bfc](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/3181bfceedb650494d162e9e2de61538de8a8fb4))
* **analysis:** add fem-review recipe turning a solve into an engineering verdict ([#60](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/60)) ([e9a1209](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/e9a1209a9e75bb11ae34feb327b35a36029cb52c))
* **analysis:** add typed FEM structural analysis with verified units ([#45](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/45)) ([c9837c4](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/c9837c4c2165da1e387f5612219fc27bbc236376))
* **modeling:** add scale, copy and mirror plus wedge, helix and 3MF ([#55](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/55)) ([ddc93a5](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/ddc93a5b14ee89942b09be9d50674aca7289e418))


### Bug Fixes

* **listing:** page over sorted results instead of walk order ([334b038](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/334b0389a1c4ffcf044880ed27eea3a3babf6bf5))
* **sketch:** export the sketch error types and document the sketch read-back contract ([#59](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/59)) ([fee865d](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/fee865d8b7a094850aa43eba1a6e98eba44310a0))

## [0.7.0](https://github.com/dcc-mcp/dcc-mcp-freecad/compare/v0.6.0...v0.7.0) (2026-10-07)


### Features

* add bounded saved-copy appearance and framing ([#37](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/37)) ([e1261e9](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/e1261e96def16f811ad9f881f0ce402d691d2198))
* add fillet, chamfer, pattern and mirror tools ([85a82a7](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/85a82a77632703b448f5ae578dd56a52f2ee03cc))
* add verifiable headless render_view snapshots ([#52](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/52)) ([63a2549](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/63a25497db3f8215396d59bc14f9afede8a21beb))
* **capabilities:** derive get_capabilities from tools.yaml and lock the contract ([#44](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/44)) ([abd87e0](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/abd87e013e6edf888f6d29e30a111a3c05e5b09e))
* **parts:** add offline standard-parts library tools ([#47](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/47)) ([6aad1ac](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/6aad1ac0fbc87974b3bd2accbd5451cda34cc0cf))
* **recoverability:** add document snapshots and restore ([#43](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/43)) ([4ae7e15](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/4ae7e15e55e050d841592b20485c42fd47a33ac9))
* **script:** add run_script escape hatch for controlled scripting ([#57](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/57)) ([2a8d84d](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/2a8d84de05dc0b532a658044ec8d8bc4ed3e2536))
* **sketch:** typed PartDesign sketch tools with a constraint gate ([#51](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/51)) ([ac51ae4](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/ac51ae45e829d07ac9e259188fd604b9d8dda51c))


### Bug Fixes

* **ci:** cut release PRs with a collaborator token ([#34](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/34)) ([96161a8](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/96161a8c6c583bfc55ef95576d688815c451486f))


### Documentation

* add AGENTS.md and adopt the repo contract gate ([#40](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/40)) ([8d7ea57](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/8d7ea57b6b984047273e8df143f5fc7c9a2af7eb))
* **readme:** add the generated DCC-MCP host matrix pointer ([#32](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/32)) ([d76a32f](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/d76a32f60a9b0a8cf650dbd4ed64ccba3b698185))

## [0.6.0](https://github.com/dcc-mcp/dcc-mcp-freecad/compare/v0.5.0...v0.6.0) (2026-10-04)


### Features

* preserve native presentation in FreeCAD document copies ([#26](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/26)) ([fa75a62](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/fa75a625fee5c7e005fe9bb856c6f44a50525b5c))


### Bug Fixes

* drop the unused Install SOP artifact revision mirror ([#31](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/31)) ([b6fd19e](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/b6fd19e3f70c4ffb9141621e92f4c31295db9398))
* fall back to an exclusive copy when the output volume has no hard links ([ff5b57a](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/ff5b57a661602301d953aa9537eda66786510461))
* publish fallback copies with the staged file's mode ([#30](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/30)) ([1fc099e](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/1fc099e529a0eefd0d9e80deceea22444f4ffe05))

## [0.5.0](https://github.com/dcc-mcp/dcc-mcp-freecad/compare/v0.4.2...v0.5.0) (2026-10-03)


### Features

* support isolated FreeCAD native-library backend ([ea2c864](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/ea2c864200bdcc41c4f3ad93c73bb97c8b667834))

## [0.4.2](https://github.com/dcc-mcp/dcc-mcp-freecad/compare/v0.4.1...v0.4.2) (2026-09-30)


### Bug Fixes

* emit the Install SOP report schema version, not the artifact revision ([#21](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/21)) ([84bc9c9](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/84bc9c93f447403451857f939b77fcaf076e861f))

## [0.4.1](https://github.com/dcc-mcp/dcc-mcp-freecad/compare/v0.4.0...v0.4.1) (2026-09-29)


### Bug Fixes

* report caller paths in write failures and refuse zero deflections ([867d3f1](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/867d3f10194ea3cacf16a6d349cf59398a7331d8))

## [0.4.0](https://github.com/dcc-mcp/dcc-mcp-freecad/compare/v0.3.0...v0.4.0) (2026-09-29)


### Features

* prove every mutating tool took effect before it reports success ([#17](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/17)) ([eb677ae](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/eb677ae678a7d0a4464af37f60b3005cdd2c4726))

## [0.3.0](https://github.com/dcc-mcp/dcc-mcp-freecad/compare/v0.2.0...v0.3.0) (2026-09-29)


### Features

* machine-readable FreeCAD host compatibility matrix and startup preflight ([#14](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/14)) ([7b8f388](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/7b8f388b74ca4eef14abf967b0069f84dc32e21b))

## [0.2.0](https://github.com/dcc-mcp/dcc-mcp-freecad/compare/v0.1.2...v0.2.0) (2026-08-25)


### Features

* add standalone FreeCAD doctor ([#9](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/9)) ([f5a59f8](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/f5a59f8222a5194a6644cf5c73a3cbff6f12b53b))

## [0.1.2](https://github.com/dcc-mcp/dcc-mcp-freecad/compare/v0.1.1...v0.1.2) (2026-08-12)


### Documentation

* add CAD interchange showcase ([#5](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/5)) ([6e83d40](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/6e83d40432bc79c75379891db808f440fbf6d9ca))
* publish approved FreeCAD showcase ([#7](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/7)) ([054963f](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/054963fae29d3bf9f31d4cd791b83ff634174dd5))

## [0.1.1](https://github.com/dcc-mcp/dcc-mcp-freecad/compare/v0.1.0...v0.1.1) (2026-08-10)


### Bug Fixes

* restore complete MIT license text ([#3](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/3)) ([2aef023](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/2aef0231f82d2e8d227fc06bc40a7f4ebd5acb28))

## 0.1.0 (2026-08-10)


### Features

* add experimental FreeCAD adapter ([325fdf9](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/325fdf9597768ecdd9e10a11af0e52edcc3839f2))
* make FreeCAD adapter production ready ([#1](https://github.com/dcc-mcp/dcc-mcp-freecad/issues/1)) ([62af5f0](https://github.com/dcc-mcp/dcc-mcp-freecad/commit/62af5f06295dae9167db66af162b3e6a3dbd61a0))
