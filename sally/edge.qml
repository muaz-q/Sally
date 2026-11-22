// The light that keeps running around the dashboard's edge, drawn by the graphics card.
// Three soft glow bands (the song's colours, pre-drawn at three angles) blend into each
// other so the colours seem to flow round, and a bright dot with a fading tail races along.
import QtQuick

Item {
    id: root

    property real u: 1
    property real radius: 8
    property real inset: 1.5
    property var bands: []         // three pictures of the glow band
    property string spark: ""
    property string halo: ""
    property real lap: 7           // seconds for the dot to go once around
    property real t: 0

    FrameAnimation {
        running: root.visible
        onTriggered: root.t += frameTime
    }

    function weight(i) {
        var p = (root.t / (root.lap * 1.6) * 3) % 3;
        var d = Math.abs(p - i);
        d = Math.min(d, 3 - d);
        return Math.max(0, 1 - d);
    }

    // a point on the rounded rectangle's outline; s runs 0..1 clockwise from the top-left
    function at(s) {
        var i = root.inset * root.u, r = root.radius;
        var w = root.width - 2 * i, h = root.height - 2 * i;
        var top = w - 2 * r, side = h - 2 * r, arc = Math.PI * r / 2;
        var d = (((s % 1) + 1) % 1) * (2 * top + 2 * side + 4 * arc);
        function corner(cx, cy, a0) {
            var a = a0 + (d / arc) * Math.PI / 2;
            return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
        }
        if (d < top) return [i + r + d, i];
        d -= top;
        if (d < arc) return corner(i + w - r, i + r, -Math.PI / 2);
        d -= arc;
        if (d < side) return [i + w, i + r + d];
        d -= side;
        if (d < arc) return corner(i + w - r, i + h - r, 0);
        d -= arc;
        if (d < top) return [i + w - r - d, i + h];
        d -= top;
        if (d < arc) return corner(i + r, i + h - r, Math.PI / 2);
        d -= arc;
        if (d < side) return [i, i + h - r - d];
        d -= side;
        return corner(i + r, i + r, Math.PI);
    }

    Repeater {
        model: root.bands.length
        delegate: Image {
            required property int index
            anchors.fill: parent
            source: root.bands[index]
            smooth: true
            opacity: root.weight(index)
            visible: opacity > 0.004
        }
    }

    Repeater {   // the tail, fading behind the dot
        model: 22
        delegate: Image {
            required property int index
            property real k: (index + 1) / 22
            property var p: root.at(root.t / root.lap - k * 0.085)
            property real r: (1.3 + 3.2 * (1 - k)) * root.u
            source: root.spark
            width: 2.6 * r; height: width
            x: p[0] - width / 2; y: p[1] - height / 2
            opacity: Math.pow(1 - k, 1.5)
            smooth: true
        }
    }

    Image {   // the dot
        property var p: root.at(root.t / root.lap)
        source: root.halo
        width: 30 * root.u; height: width
        x: p[0] - width / 2; y: p[1] - height / 2
        smooth: true
    }
}
