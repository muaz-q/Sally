// Sally's stage: the cover and its moving neon rings, drawn by the graphics card.
// Python (widgets.Stage) feeds it pre-drawn ring pictures and, during a song change,
// the transition values; the endless motion itself runs here at the screen's refresh rate.
import QtQuick
import QtQuick.Effects

Item {
    id: root

    property real u: 1
    property real cx: width / 2
    property real cy: 360 * u
    property real t: 0                  // seconds of motion, advanced every frame

    // rings: [{a, b, angle, spin, phase, dot, strength, w, h}] in logical pixels
    property var rings: []
    property var srcA: []               // ring pictures in the current colours
    property var srcB: []               // ...and in the incoming song's colours
    property string sparkA: ""
    property string sparkB: ""
    property real mix: 0                // 0 = colours A, 1 = colours B
    property color haloColor: "#40aaff"
    property real fadeFrom: 0.8         // where the rings start fading (fraction of height)
    property real fadeTo: 1.0

    // song change
    property real trans: -1             // seconds since the change began; < 0 when idle
    property var particles: []          // [{born, life, r0, th0, turn, colour, size}]
    property var sparkTo: []            // spark pictures for the three new colours
    property string cover: ""           // the cover (or the current frame of the swirl)
    property real coverSide: 216 * u

    // your top tracks: each rides its own orbit (a resized, re-tilted copy of one of the
    // rings, drawn faintly so you can see it), grows out from behind the cover, circles,
    // then its orbit shrinks and it sinks back behind the cover; then the next seven come
    property var tracks: []             // [{uri, title, subtitle, img}]; img is a round, framed cover
    property string trackGlow: ""
    property bool showTracks: true
    property real tt: 0                 // the tracks' own clock: it stands still while you hover one
    property real start: 0              // tt when this set of tracks arrived
    property bool holding: false
    property int hovered: -1            // which cover the mouse is on; its name shows beside it
    property string hoverTitle: ""
    property string hoverArtist: ""
    property color labelEdge: "#c8a0ff"
    property real hoverX: 0             // the hovered cover's centre and radius, kept up to date by it
    property real hoverY: 0
    property real hoverR: 0
    property var hits: []               // [[x, y, r], ...] of covers you can click, for Python
    readonly property int per: showTracks ? Math.min(7, tracks.length) : 0
    readonly property int pages: Math.max(1, Math.ceil(tracks.length / 7))
    readonly property real cycle: 44
    readonly property real emerge: 10    // growing out is slow...
    readonly property real sinkTime: 5   // ...sinking in twice as fast
    readonly property real inner: 0.3    // orbit scale where a cover has gone behind the art
    readonly property int batch: Math.floor((tt - start) / cycle)
    readonly property int page: batch % pages

    signal play(string uri)
    signal hover(string text)

    function rand(seed) { var x = Math.sin(seed * 12.9898 + 78.233) * 43758.5453; return x - Math.floor(x); }
    function ease(x) { x = Math.max(0, Math.min(1, x)); return x * x * (3 - 2 * x); }

    // cover i's orbit for this batch, picked at random
    function orbit(i) {
        var seed = batch * 31 + i * 7 + 1;
        var ri = (i + Math.floor(rand(seed) * rings.length)) % rings.length;
        var g = rings[ri];
        var delay = rand(seed + 3) * 3;
        return {
            ri: ri, ring: g,
            fa: 0.84 + 0.26 * rand(seed + 5), fb: 0.8 + 0.5 * rand(seed + 6),
            angle: g.angle + (rand(seed + 7) - 0.5) * 50,
            spin: g.spin * (0.7 + 0.6 * rand(seed + 8)),
            phase0: rand(seed + 1) * 2 * Math.PI,
            speed: g.dot * (0.55 + 0.3 * rand(seed + 2)),
            delay: delay,
            stay: cycle - 0.6 - delay - emerge - sinkTime - rand(seed + 4) * 7
        };
    }

    // where cover i is now: {x, y (from the centre), r, k (orbit scale), wob, rot, grow}
    function trackAt(o) {
        var s = tt - start - batch * cycle - o.delay;
        var k, grow;
        if (s < 0) { k = inner; grow = 0; }
        else if (s < emerge) { var e = ease(s / emerge); k = inner + (1 - inner) * e; grow = e; }
        else if (s < emerge + o.stay) { k = 1; grow = 1; }
        else { var d = ease((s - emerge - o.stay) / sinkTime); k = 1 - (1 - inner) * d; grow = 1 - d; }
        var dir = o.speed < 0 ? -1 : 1;
        // a shrinking orbit whirls faster, like water down a drain
        var phi = o.phase0 + o.speed * (tt - start) + dir * 1.8 * (1 - k) * (1 - k);
        var wob = 1 + 0.12 * Math.sin(t * 0.45 + o.ring.phase);
        var ex = k * o.fa * o.ring.a * Math.cos(phi);
        var ey = k * o.fb * o.ring.b * wob * Math.sin(phi);
        var rot = o.angle + o.spin * t;
        var rr = rot * Math.PI / 180;
        return {x: ex * Math.cos(rr) - ey * Math.sin(rr), y: ex * Math.sin(rr) + ey * Math.cos(rr),
                r: 22 * u * grow, k: k, wob: wob, rot: rot, grow: grow};
    }

    function sstep(a, b, x) {
        var k = Math.max(0, Math.min(1, (x - a) / (b - a)));
        return k * k * (3 - 2 * k);
    }

    FrameAnimation {
        running: true
        property int n: 0
        onTriggered: {
            root.t += frameTime;
            if (!root.holding)
                root.tt += frameTime;
            if (root.per > 0 && ++n % 2 == 0) {   // which covers can be clicked, ~30 times a second
                var h = [], half = root.coverSide / 2, low = root.height * (root.fadeFrom + root.fadeTo) / 2;
                for (var i = 0; i < root.per; i++) {
                    var p = root.trackAt(root.orbit(i));
                    var behindArt = Math.abs(p.x) < half - p.r * 0.4 && Math.abs(p.y) < half - p.r * 0.4;
                    if (p.r > 4 && !behindArt && root.cy + p.y < low)
                        h.push([root.cx + p.x, root.cy + p.y, p.r]);
                }
                root.hits = h;
            } else if (root.per == 0 && root.hits.length) {
                root.hits = [];
            }
        }
    }

    // everything that glows, faded out above the title through a gradient mask
    Item {
        id: glow
        anchors.fill: parent
        layer.enabled: true
        layer.effect: MultiEffect {
            maskEnabled: true
            maskSource: fadeMask
            maskThresholdMin: 0.3
            maskSpreadAtMin: 0.4     // a smooth fade that is fully clear before the edge (the defaults cut hard)
        }

        Repeater {
            model: [[330, 0.09, 1.2], [172, 0.13, 1.0], [152, 0.22, 1.4]]
            delegate: Rectangle {
                required property var modelData
                width: 2 * modelData[0] * root.u
                height: width
                radius: width / 2
                x: root.cx - width / 2
                y: root.cy - height / 2
                color: "transparent"
                antialiasing: true
                border.width: modelData[2] * root.u
                border.color: Qt.rgba(root.haloColor.r, root.haloColor.g, root.haloColor.b, modelData[1])
            }
        }

        Repeater {
            model: root.rings.length
            delegate: Item {
                id: ring
                required property int index
                property var d: root.rings[index]
                property real k: 1 + 0.12 * Math.sin(root.t * 0.45 + d.phase)   // slow 3D-like tilt
                x: root.cx
                y: root.cy
                rotation: d.angle + d.spin * root.t

                Item {
                    transform: Scale { yScale: ring.k }
                    Image {
                        x: -ring.d.w / 2; y: -ring.d.h / 2
                        width: ring.d.w; height: ring.d.h
                        source: root.srcA[ring.index] || ""
                        opacity: 1 - root.mix
                        visible: opacity > 0.004
                        smooth: true
                    }
                    Image {
                        x: -ring.d.w / 2; y: -ring.d.h / 2
                        width: ring.d.w; height: ring.d.h
                        source: root.srcB[ring.index] || ""
                        opacity: root.mix
                        visible: opacity > 0.004
                        smooth: true
                    }
                }

                Repeater {   // two sparks riding each ring
                    model: 2
                    delegate: Item {
                        required property int index
                        property real phi: ring.d.phase + index * Math.PI + root.t * ring.d.dot
                        x: ring.d.a * Math.cos(phi)
                        y: ring.d.b * ring.k * Math.sin(phi)
                        Image {
                            width: 20 * root.u; height: width; x: -width / 2; y: -height / 2
                            source: root.sparkA
                            opacity: 0.9 * ring.d.strength * (1 - root.mix)
                            visible: opacity > 0.004
                            smooth: true
                        }
                        Image {
                            width: 20 * root.u; height: width; x: -width / 2; y: -height / 2
                            source: root.sparkB
                            opacity: 0.9 * ring.d.strength * root.mix
                            visible: opacity > 0.004
                            smooth: true
                        }
                    }
                }
            }
        }

        // your top tracks, each with its own faint orbit
        Repeater {
            model: root.rings.length > 0 ? root.per : 0
            delegate: Item {
                id: tr
                required property int index
                property var o: { root.batch; return root.orbit(index); }
                property var p: { root.t; root.tt; return root.trackAt(o); }
                property var track: root.tracks[(root.page * 7 + index) % root.tracks.length]
                property real r: Math.max(0.01, p.r)
                onPChanged: if (root.hovered === index) tr.report()
                function report() { root.hoverX = root.cx + p.x; root.hoverY = root.cy + p.y; root.hoverR = p.r; }

                Item {   // the orbit
                    x: root.cx; y: root.cy
                    rotation: tr.p.rot
                    opacity: 0.42 * tr.p.grow
                    visible: opacity > 0.01
                    Item {
                        transform: Scale { xScale: tr.p.k * tr.o.fa; yScale: tr.p.k * tr.o.fb * tr.p.wob }
                        Image {
                            x: -tr.o.ring.w / 2; y: -tr.o.ring.h / 2
                            width: tr.o.ring.w; height: tr.o.ring.h
                            source: root.srcA[tr.o.ri] || ""
                            opacity: 1 - root.mix
                            visible: opacity > 0.004
                            smooth: true
                        }
                        Image {
                            x: -tr.o.ring.w / 2; y: -tr.o.ring.h / 2
                            width: tr.o.ring.w; height: tr.o.ring.h
                            source: root.srcB[tr.o.ri] || ""
                            opacity: root.mix
                            visible: opacity > 0.004
                            smooth: true
                        }
                    }
                }

                Item {   // the cover
                    x: root.cx + tr.p.x; y: root.cy + tr.p.y
                    visible: tr.p.r > 0.5
                    opacity: Math.min(1, tr.p.r / (9 * root.u))
                    scale: area.containsMouse ? 1.12 : 1
                    Behavior on scale { NumberAnimation { duration: 120 } }
                    Image {
                        width: 3.1 * tr.r; height: width; x: -width / 2; y: -height / 2
                        source: root.trackGlow; smooth: true
                        opacity: area.containsMouse ? 1 : 0.65
                    }
                    Image {
                        id: art
                        width: 2 * tr.r; height: width; x: -tr.r; y: -tr.r
                        source: tr.track ? tr.track.img : ""; smooth: true
                    }
                    MouseArea {
                        id: area
                        anchors.fill: art
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.play(tr.track.uri)
                        onEntered: {
                            root.holding = true;
                            root.hoverTitle = tr.track.title;
                            root.hoverArtist = tr.track.subtitle;
                            root.hovered = tr.index;
                            tr.report();
                        }
                        onExited: { root.holding = false; root.hovered = -1; }
                    }
                }
            }
        }

        // sparks peel off the rings and spiral into the cover
        Repeater {
            model: root.particles.length
            delegate: Item {
                id: pa
                required property int index
                property var q: root.particles[index] || {born: 0, life: 1, r0: 0, th0: 0, turn: 0, colour: 0, size: 1}
                property real s: root.trans < 0 ? -1 : (root.trans - q.born) / q.life
                visible: s > 0 && s < 1
                Repeater {
                    model: 4
                    delegate: Image {
                        required property int index
                        property real sj: pa.s - index * 0.04
                        property real e: sj * sj            // accelerate inwards, like being pulled in
                        property real r: pa.q.r0 * (1 - e) + root.coverSide * 0.5 * e
                        property real th: pa.q.th0 + pa.q.turn * sj
                        property real sz: pa.q.size * (1 - index / 6)
                        visible: sj > 0
                        source: root.sparkTo[pa.q.colour] || ""
                        width: 2 * sz; height: width
                        x: root.cx + r * Math.cos(th) - sz
                        y: root.cy + r * Math.sin(th) * 0.8 - sz
                        opacity: (1 - index / 4) * Math.min(1, pa.s * 6) * (1 - 0.6 * root.sstep(0.85, 1, pa.s))
                        smooth: true
                    }
                }
            }
        }
    }

    Rectangle {
        id: fadeMask
        anchors.fill: parent
        visible: false
        layer.enabled: true
        gradient: Gradient {
            GradientStop { position: 0; color: "white" }
            GradientStop { position: root.fadeFrom; color: "white" }
            GradientStop { position: root.fadeTo; color: "transparent" }
        }
    }

    Image {
        x: root.cx - root.coverSide / 2
        y: root.cy - root.coverSide / 2
        width: root.coverSide
        height: root.coverSide
        source: root.cover
        cache: false
        smooth: true
    }

    // the hovered song's name, in a small glass label beside its cover
    Rectangle {
        id: label
        objectName: "hoverLabel"
        visible: opacity > 0.01
        opacity: root.hovered >= 0 ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: 140 } }
        width: Math.min(230 * root.u, Math.max(titleText.implicitWidth, artistText.implicitWidth) + 24 * root.u)
        height: 46 * root.u
        radius: 12 * root.u
        color: "#e6141028"
        border.width: 1
        border.color: Qt.rgba(root.labelEdge.r, root.labelEdge.g, root.labelEdge.b, 0.7)
        antialiasing: true
        // to the right of the cover, or to its left near the right edge; never outside the stage
        property real gap: root.hoverR * 1.12 + 10 * root.u
        property real wantX: root.hoverX + gap + width < root.width - 6 * root.u
                             ? root.hoverX + gap : root.hoverX - gap - width
        x: Math.max(6 * root.u, Math.min(root.width - width - 6 * root.u, wantX))
        y: Math.max(6 * root.u, Math.min(root.height - height - 6 * root.u, root.hoverY - height / 2))

        Text {
            id: titleText
            x: 12 * root.u; y: 6 * root.u
            width: label.width - 24 * root.u
            text: root.hoverTitle; elide: Text.ElideRight
            color: "white"; font.family: "Segoe UI"; font.pixelSize: 13 * root.u; font.weight: Font.DemiBold
        }
        Text {
            id: artistText
            x: 12 * root.u; y: 25 * root.u
            width: label.width - 24 * root.u
            text: root.hoverArtist; elide: Text.ElideRight
            color: "#b4ffffff"; font.family: "Segoe UI"; font.pixelSize: 11.5 * root.u
        }
    }
}
