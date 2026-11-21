package com.aaditpani.nora.link

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ParamCheckTest {
    private val fences = JSONObject("""
        {"type":"object","required":["fences"],
         "properties":{"fences":{"type":"array","maxItems":2,"items":{
           "type":"object","required":["id","lat","lon"],
           "properties":{"id":{"type":"string","maxLength":40},
                         "lat":{"type":"number","minimum":-90,"maximum":90},
                         "lon":{"type":"number","minimum":-180,"maximum":180},
                         "radius_m":{"type":"number","minimum":50,"maximum":5000}}}}}}""")

    private fun check(params: String) = ParamCheck.validate(fences, JSONObject(params))

    @Test
    fun `a list of places fits, and so does an empty one`() {
        assertNull(check("""{"fences":[{"id":"home","lat":12.97,"lon":77.59,"radius_m":150}]}"""))
        assertNull(check("""{"fences":[{"id":"home","lat":12,"lon":77}]}"""))
        assertNull(check("""{"fences":[]}"""))
    }

    @Test
    fun `nothing unknown gets in at any depth`() {
        assertEquals("unknown field 'fences[0].extra'",
            check("""{"fences":[{"id":"home","lat":1.0,"lon":2.0,"extra":true}]}"""))
        assertEquals("unknown field 'other'", check("""{"fences":[],"other":1}"""))
    }

    @Test
    fun `limits hold inside the list`() {
        assertEquals("'fences[0].lat' is too large", check("""{"fences":[{"id":"x","lat":91.5,"lon":0}]}"""))
        assertEquals("'fences[0].lon' is required", check("""{"fences":[{"id":"x","lat":1}]}"""))
        assertEquals("'fences[0].lat' must be a number", check("""{"fences":[{"id":"x","lat":"north","lon":0}]}"""))
        assertEquals("'fences' has more than 2 items",
            check("""{"fences":[{"id":"a","lat":0,"lon":0},{"id":"b","lat":0,"lon":0},{"id":"c","lat":0,"lon":0}]}"""))
        assertEquals("'fences' must be a list", check("""{"fences":{"id":"a"}}"""))
    }

    @Test
    fun `the flat schemas the other capabilities use still check as before`() {
        val alarm = JSONObject("""{"type":"object","required":["hour"],
            "properties":{"hour":{"type":"integer","minimum":0,"maximum":23}}}""")
        assertNull(ParamCheck.validate(alarm, JSONObject("""{"hour":7}""")))
        assertEquals("'hour' must be an integer", ParamCheck.validate(alarm, JSONObject("""{"hour":7.5}""")))
        assertEquals("'hour' is too large", ParamCheck.validate(alarm, JSONObject("""{"hour":24}""")))
    }
}
