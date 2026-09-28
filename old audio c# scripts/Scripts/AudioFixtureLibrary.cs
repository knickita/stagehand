using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class AudioFixtureLibrary : MonoBehaviour
{
    public List<AudioFixture> library;

    void Start(){
        library = new List<AudioFixture>();
        AudioFixture[] fixtures = GetComponentsInChildren<AudioFixture>();
        foreach (AudioFixture fixture in fixtures){
            library.Add(fixture);
        }
    }
}
